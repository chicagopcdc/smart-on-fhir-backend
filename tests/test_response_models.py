"""The response models and the normalization layer must not drift apart.

``app/api/schemas.py`` restates the envelope ``app/fhir/normalize.py`` produces,
because that is what generates the OpenAPI document. Two descriptions of one
shape can disagree, and the failure mode is quiet: a field the model does not
declare is dropped from the response without complaint, so the documentation and
the data stay plausible while no longer matching.

So the models are validated against real normalizer output — including the shape
of a read that failed, which is the one a consumer is least likely to have tried
and most likely to be surprised by.

The generated document is checked here too, for a failure one step earlier: a
route that declares no model at all. FastAPI documents that as an empty schema
rather than refusing it, so the endpoint keeps working and `/docs` silently tells
a caller nothing. This is the same fail-fast instinct as
``validate_us_core_membership`` and ``validate_section_sources``, applied to the
HTTP surface instead of the fetch config.
"""

from __future__ import annotations

import pytest

from app import main
from app.api.schemas import ResourceEnvelope
from app.fhir import normalize
from app.providers import config
from tests.app_harness import load_fixture

RECORDS = ["launcher_patient_record.json", "cerner_patient_record.json"]


@pytest.mark.parametrize("record_name", RECORDS)
def test_every_captured_response_validates_against_the_envelope(record_name):
    record = load_fixture(record_name)
    checked = 0

    for name, response in record["responses"].items():
        envelope = normalize.normalize_response(
            response["body"],
            fhir_type=config.fhir_type_for(config.RESOURCE_FETCH_CONFIG[name]),
            status_code=response["statusCode"],
        )
        # The model must accept it without dropping anything, so compare what
        # comes back out against what went in.
        validated = ResourceEnvelope.model_validate(envelope).model_dump(by_alias=True)
        assert validated == envelope, name
        checked += 1

    assert checked, f"{record_name} carried no responses to check"


def test_a_failed_read_validates_too():
    envelope = normalize.normalize_failure(
        fhir_type="Condition", status_code=503, body=None
    )

    validated = ResourceEnvelope.model_validate(envelope).model_dump(by_alias=True)

    assert validated == envelope
    assert validated["status"] == "error"


def test_an_undeclared_status_is_refused():
    """The envelope's status is a closed set, so a third value is a contract
    change rather than something to pass through."""
    envelope = normalize.normalize_failure(
        fhir_type="Condition", status_code=503, body=None
    )
    envelope["status"] = "partial"

    with pytest.raises(ValueError):
        ResourceEnvelope.model_validate(envelope)


def _documented_success_schema(operation: dict) -> dict:
    content = operation.get("responses", {}).get("200", {}).get("content", {})
    return content.get("application/json", {}).get("schema", {})


def test_every_supported_route_documents_what_it_returns():
    """A live route with no response model is undocumented, not merely untyped.

    The deprecated aliases are exempt: they exist to keep the current frontend
    working and answer their original hand-built shapes, which is precisely why
    they are marked deprecated rather than modelled.
    """
    undocumented = [
        f"{method.upper()} {path}"
        for path, operations in main.app.openapi()["paths"].items()
        for method, operation in operations.items()
        if not operation.get("deprecated") and not _documented_success_schema(operation)
    ]

    assert not undocumented, f"routes returning an undocumented shape: {undocumented}"
