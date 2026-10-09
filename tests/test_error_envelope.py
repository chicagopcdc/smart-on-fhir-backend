"""The error contract: one shape, a closed set of codes, and a document to match.

The per-flow tests each assert the code and sentence their own refusal answers
with. What they cannot see is the whole: that no route answers a shape of its
own, that no code is undocumented, and that the OpenAPI document describes what
a caller will actually receive. That is what is checked here, by walking the
application rather than by listing the routes again — a list would go stale the
first time one was added.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.api.errors import MEANINGS, ErrorCode
from app.main import app
from tests.app_harness import (
    SMART_LAUNCHER as LAUNCHER,
    app_db,
    bearer,
    client,
    refusal,
)

ERROR_REF = "#/components/schemas/ErrorResponse"


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'errors.db'}"


def openapi() -> dict:
    """The generated document, rebuilt rather than read off the cache.

    ``app.openapi_schema`` is memoized, and another test in the same session may
    have built it already. Clearing it first means this reads the document the
    current code produces.
    """
    app.openapi_schema = None
    try:
        return app.openapi()
    finally:
        app.openapi_schema = None


def operations():
    """Every (method, path, operation) the document describes."""
    for path, methods in openapi()["paths"].items():
        for method, operation in methods.items():
            yield method, path, operation


# --- the codes ----------------------------------------------------------------


def test_every_code_says_what_it_means():
    """A code with no line explaining it is a code a caller has to guess at."""
    assert [code.value for code in ErrorCode if not MEANINGS.get(code)] == []


def test_codes_are_spelled_the_way_they_are_named():
    """The wire value and the member are the same string, always.

    They are two spellings of one thing, and a member whose value drifted from
    its name would answer a code no reader of this enum expects.
    """
    assert [code.value for code in ErrorCode] == [code.name for code in ErrorCode]
    assert all(code.value.isupper() for code in ErrorCode)


# --- the document -------------------------------------------------------------


def test_every_documented_failure_describes_the_envelope():
    """No endpoint documents an error body of its own invention.

    Walks the document rather than the routers, so an endpoint added later with a
    hand-rolled ``responses`` entry fails here instead of shipping a second shape.
    ``/health`` is the deliberate exception and says why below.
    """
    for method, path, operation in operations():
        for status, response in operation["responses"].items():
            if not status.startswith(("4", "5")):
                continue
            if path == "/health":
                continue  # see test_health_reports_its_own_state_rather_than_refusing
            schema = response["content"]["application/json"]["schema"]
            assert schema == {"$ref": ERROR_REF}, f"{method} {path} {status}"


def test_every_endpoint_documents_the_failure_it_cannot_model():
    """A 500 is answered by the middleware, so no route would think to declare it."""
    for method, path, operation in operations():
        assert "500" in operation["responses"], f"{method} {path}"


def test_documented_examples_are_codes_this_api_can_answer():
    """An example naming a code that does not exist is documentation that lies."""
    known = {code.value for code in ErrorCode}
    for method, path, operation in operations():
        for status, response in operation["responses"].items():
            examples = response.get("content", {}).get("application/json", {}).get(
                "examples", {}
            )
            for name, example in examples.items():
                code = example["value"]["error"]["code"]
                assert code in known, f"{method} {path} {status} {name}"
                assert code == name, "the example is keyed by the code it shows"


def test_fastapis_own_error_shapes_are_gone_from_the_document():
    """Nothing answers ``HTTPValidationError`` any more, so nothing should describe it."""
    schemas = openapi()["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas
    assert "ErrorResponse" in schemas and "ErrorDetail" in schemas


def test_the_code_field_is_documented_as_the_closed_set_it_is():
    """A generated client should be able to switch on the code exhaustively."""
    schemas = openapi()["components"]["schemas"]
    ref = schemas["ErrorDetail"]["properties"]["code"]["$ref"].rsplit("/", 1)[-1]
    assert set(schemas[ref]["enum"]) == {code.value for code in ErrorCode}


# --- what is actually served --------------------------------------------------


async def test_a_path_that_does_not_exist_is_refused_in_shape(db_url):
    """The router's own 404 goes through the same envelope as everyone else's."""
    async with app_db(db_url):
        async with client() as http:
            response = await http.get("/no-such-route")

    assert response.status_code == 404
    assert refusal(response) == ("NOT_FOUND", "Not Found")


async def test_a_method_a_route_does_not_take_is_refused_in_shape(db_url):
    """So does the 405, which is raised by the router and not by any endpoint."""
    async with app_db(db_url):
        async with client() as http:
            response = await http.get("/auth/connect")

    assert response.status_code == 405
    assert refusal(response)[0] == "METHOD_NOT_ALLOWED"


async def test_a_malformed_request_is_refused_in_shape(db_url):
    """FastAPI's validation 422 was the one body in the API that was a list."""
    async with app_db(db_url):
        async with client() as http:
            response = await http.post("/auth/connect", json={"provider": "EPIC_SANDBOX"})

    assert response.status_code == 422
    code, message = refusal(response)
    assert code == "INVALID_REQUEST"
    # Names the field at fault, which is what makes the message actionable.
    assert "iss" in message


async def test_a_validation_failure_does_not_read_the_input_back_out(db_url):
    """The field and the complaint, never the value — the same rule the rest keeps.

    FastAPI's own 422 body carries an ``input`` for every error. On this API an
    input is as likely to be a patient identifier as a page number, and a
    response that quotes one is a response that puts it in a browser's console,
    a proxy log, and a bug report.
    """
    secret = "pat_not_to_be_repeated"
    async with app_db(db_url):
        async with client() as http:
            response = await http.get(
                "/providers/search", params={"pageSize": secret}
            )

    assert response.status_code == 422
    assert secret not in response.text


@respx.mock
async def test_an_unreachable_provider_and_a_refused_grant_differ_by_code(db_url):
    """Two 502s from one route, told apart by something other than the sentence.

    The pair this ticket exists for: before, a caller reading `/auth/connect`
    could tell "unknown provider" from "provider not configured" only by the
    status, and the two 502s of `/auth/callback` not at all.
    """
    respx.get(LAUNCHER["iss"] + "/.well-known/smart-configuration").mock(
        side_effect=httpx.ConnectError("down")
    )
    async with app_db(db_url):
        async with client() as http:
            unknown = await http.post(
                "/auth/connect",
                json={"provider": "NOT_A_PROVIDER", "iss": LAUNCHER["iss"]},
            )
            unreachable = await http.post(
                "/auth/connect",
                json={"provider": LAUNCHER["provider"], "iss": LAUNCHER["iss"]},
            )

    assert refusal(unknown)[0] == "UNKNOWN_PROVIDER"
    assert unreachable.status_code == 502
    assert refusal(unreachable)[0] == "PROVIDER_UNREACHABLE"


async def test_a_missing_bearer_is_told_from_a_bad_one(db_url):
    """Both are 401, and only one of them means "try signing in again"."""
    async with app_db(db_url):
        async with client() as http:
            missing = await http.get("/auth/me")
            bad = await http.get("/auth/me", headers=bearer("not-a-session"))

    assert missing.status_code == bad.status_code == 401
    assert refusal(missing)[0] == "AUTHENTICATION_REQUIRED"
    assert refusal(bad)[0] == "SESSION_INVALID"


async def test_health_reports_its_own_state_rather_than_refusing(db_url):
    """The one 5xx that is not an error envelope, on purpose.

    ``/health`` answers a ``HealthResponse`` whether it is 200 or 503, because a
    container runtime reads the status and an operator reads the fields. Wrapping
    that in a refusal envelope would drop ``status`` and ``database`` to say
    nothing a probe can act on — and the probe in ``docker-compose.yml`` never
    reads the body at all.
    """
    async with app_db(db_url):
        async with client() as http:
            response = await http.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
