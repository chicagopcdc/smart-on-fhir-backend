"""The swallowed-exception branch logs, and logs nothing sensitive."""

from __future__ import annotations

import httpx
import pytest

from app.api import patients
from app.auth.models import ProviderToken

FHIR_PATIENT_ID = "erXuFYUfucBZaryVksYEcMg3"


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
    """A raise the fetch layer does not model leaves a record readable, and leaves
    a trace. Without the log, the only evidence is a generic string the operator
    cannot act on."""

    async def boom(access_token, base_url, fhir_patient_id, resource_types):
        # httpx puts the request URL in the message, and ours carries the id.
        raise httpx.ConnectError(
            f"[Errno 11001] getaddrinfo failed for "
            f"https://fhir.epic.example/api/FHIR/R4/Condition?patient={fhir_patient_id}"
        )

    monkeypatch.setattr(patients.service, "fetch_fhir_resources", boom)

    with caplog.at_level("ERROR", logger="app.api.patients"):
        reads = await patients._read_all([connection], {"Condition": {}})

    assert len(reads) == 1
    assert reads[0].status == "error"
    assert reads[0].error == "This provider could not be read"

    assert len(caplog.records) == 1, "the failure left no trace"
    logged = caplog.text
    assert "ConnectError" in logged, "the log does not say what went wrong"
    assert "EPIC_SANDBOX" in logged
    assert "pat_QAtestrecordid" in logged

    # The point of logging the type rather than the message.
    assert FHIR_PATIENT_ID not in logged, "a provider-issued patient id reached the log"
    assert "getaddrinfo" not in logged, "the exception message reached the log"
