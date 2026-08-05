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
route that declares no model, or that can be throttled without saying so. FastAPI
documents either as an empty schema rather than refusing it, so the endpoint
keeps working and `/docs` silently tells a caller nothing.

These stay tests rather than import-time assertions like
``validate_us_core_membership``. Those guard behaviour — a name that matches no
fetch config row drops a resource from every read — so they are worth refusing to
boot over. This guards the published contract: the bytes are right and only the
documentation is poorer, which belongs in CI rather than in a startup path.
"""

from __future__ import annotations

import pytest

from app import main
from app.api import deps
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


def _throttled_paths() -> set[str]:
    """The routes carrying a rate limit, read from the limiter's own registries.

    Derived rather than listed, so a route that gains the decorator is covered
    without anyone remembering to add it here. Both registries are read because
    slowapi files a limit under whichever it is: this codebase passes callables
    so its routes land in the dynamic one, but a limit written as a literal
    (`@limiter.limit("5/minute")`) goes to the static one, and reading only the
    first would leave that route silently outside this check.

    These are slowapi internals, which is why the derivation lives in the suite:
    an upgrade that moves them fails loudly in CI rather than leaving `/docs`
    quietly wrong.
    """
    limited = {*deps.limiter._dynamic_route_limits, *deps.limiter._route_limits}
    return {
        route.path
        for route in main.app.routes
        if (endpoint := getattr(route, "endpoint", None)) is not None
        and f"{endpoint.__module__}.{endpoint.__name__}" in limited
    }


def test_every_throttled_route_says_it_can_refuse():
    """A route that can answer 429 without documenting it surprises its caller.

    The throttle is applied by a decorator and documented by hand, so the two can
    drift apart silently — nothing in FastAPI relates them. Deprecated routes are
    exempt for the same reason as above.
    """
    paths = _throttled_paths()
    assert paths, "no throttled route found; the limiter registry moved"

    silent = [
        f"{method.upper()} {path}"
        for path, operations in main.app.openapi()["paths"].items()
        if path in paths
        for method, operation in operations.items()
        if not operation.get("deprecated") and "429" not in operation.get("responses", {})
    ]

    assert not silent, f"throttled routes that do not document 429: {silent}"
