"""The swallowed-exception branch logs, and logs nothing sensitive.

``_read_all`` gathers with ``return_exceptions=True`` so one provider's failure
does not sink the others. What it must not do is discard the failure entirely:
the caller is told only "This provider could not be read", so the log is the sole
record of what happened.

The exception modelled here is a validation error, because that is what can
actually arrive. ``fetch_single_resource`` already catches everything the HTTP
call raises and turns it into a result dict, so a transport error never reaches
``_read_all`` — normalization runs after that guard, and pydantic quotes the
value of the field that failed.
"""

from __future__ import annotations

import pytest
from fhir.resources.R4B.patient import Patient
from pydantic import ValidationError

from app.api import patients
from app.auth.models import ProviderToken

BIRTH_DATE = "1986-01-01"
FHIR_PATIENT_ID = "erXuFYUfucBZaryVksYEcMg3"


def _validation_error() -> ValidationError:
    """A real pydantic failure over a Patient, so the message is the real shape."""
    try:
        Patient(
            resourceType="Patient",
            id=FHIR_PATIENT_ID,
            birthDate=f"{BIRTH_DATE}-corrupted-by-the-provider",
        )
    except ValidationError as exc:
        return exc
    raise AssertionError("the resource was expected to fail validation")


@pytest.fixture
def connection():
    return ProviderToken(
        patient_id="pat_QAtestrecordid",
        patient_fhir_id=FHIR_PATIENT_ID,
        provider="EPIC_SANDBOX",
        iss="https://fhir.epic.example/api/FHIR/R4",
    )


async def test_an_unmodelled_failure_is_logged_and_isolated(
    connection, monkeypatch, caplog
):
    error = _validation_error()
    # The premise: the message really does quote the patient's data back.
    assert BIRTH_DATE in str(error), "the fixture no longer proves anything"

    async def boom(access_token, base_url, fhir_patient_id, resource_types, scope=None):
        raise error

    monkeypatch.setattr(patients.service, "fetch_fhir_resources", boom)

    with caplog.at_level("ERROR", logger="app.api.patients"):
        reads = await patients._read_all([connection], {"Patient": {}})

    assert len(reads) == 1
    assert reads[0].status == "error"
    assert reads[0].error == "This provider could not be read"

    assert len(caplog.records) == 1, "the failure left no trace"
    logged = caplog.text
    assert "ValidationError" in logged, "the log does not say what went wrong"
    assert "EPIC_SANDBOX" in logged
    assert "pat_QAtestrecordid" in logged

    # The whole point of logging the type rather than the message.
    assert BIRTH_DATE not in logged, "the patient's date of birth reached the log"
    assert FHIR_PATIENT_ID not in logged, "a provider-issued id reached the log"


async def test_one_connection_failing_does_not_disturb_the_others(
    connection, monkeypatch, caplog
):
    """The reason the branch exists at all: results stay paired to connections.

    A single-connection test cannot catch a mis-pairing, so this drives two and
    checks each outcome landed on the one it belongs to.
    """
    healthy = ProviderToken(
        patient_id="pat_QAtestrecordid",
        patient_fhir_id="cerner-patient",
        provider="CERNER_SANDBOX",
        iss="https://fhir.cerner.example/r4",
    )
    error = _validation_error()

    async def selective(
        access_token, base_url, fhir_patient_id, resource_types, scope=None
    ):
        if fhir_patient_id == FHIR_PATIENT_ID:
            raise error
        return {
            "Patient": {
                "resourceType": "Patient",
                "status": "ok",
                "statusCode": 200,
                "count": 1,
                "total": 1,
                "truncated": False,
                "skipped": 0,
                "error": None,
                "entries": [],
            }
        }

    monkeypatch.setattr(patients.service, "fetch_fhir_resources", selective)

    with caplog.at_level("ERROR", logger="app.api.patients"):
        reads = await patients._read_all([connection, healthy], {"Patient": {}})

    assert [read.token.provider for read in reads] == ["EPIC_SANDBOX", "CERNER_SANDBOX"]
    assert reads[0].status == "error"
    assert reads[1].status == "ok"
    # The failure is reported against the connection that actually failed.
    assert "EPIC_SANDBOX" in caplog.text
    assert "CERNER_SANDBOX" not in caplog.text
