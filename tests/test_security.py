"""Security posture of the HTTP surface: CORS and rate limiting.

Driven against the live ASGI app so the middleware stack is exercised exactly as
it is in production.
"""

from __future__ import annotations

import pytest
import respx

from app.api import deps
from app.core.config import get_settings
from tests.app_harness import (
    SMART_LAUNCHER,
    app_db,
    mock_server,
    token_response,
)
from tests.app_harness import client as _client

# Matches FRONTEND_HOSTNAME set for the test environment in conftest.
FRONTEND_ORIGIN = "http://localhost:3000"

# Refused at the allowlist before any discovery or database work, so a test can
# spend the rate limit without standing up a server to answer.
UNKNOWN_PROVIDER = {"provider": "NOPE", "iss": "https://example.org/fhir"}


@pytest.fixture
def low_auth_rate_limit(monkeypatch):
    """Turn the limiter on with a tiny budget for one test, then restore it.

    The suite runs with rate limiting disabled (conftest) so request-heavy flow
    tests are not throttled; this fixture scopes an enabled, low limit to a
    single test and resets the shared counter on the way in and out.
    """
    monkeypatch.setenv("AUTH_RATE_LIMIT", "2/minute")
    get_settings.cache_clear()
    deps.limiter.enabled = True
    deps.limiter.reset()
    yield
    deps.limiter.enabled = False
    deps.limiter.reset()
    get_settings.cache_clear()


async def test_cors_allows_the_configured_frontend_origin():
    async with _client() as client:
        response = await client.options(
            "/auth/start",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.headers.get("access-control-allow-origin") == FRONTEND_ORIGIN


async def test_cors_rejects_an_unknown_origin():
    async with _client() as client:
        response = await client.options(
            "/auth/start",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    # A wildcard policy would echo the attacker's origin back; a locked-down one
    # simply does not grant it.
    assert "access-control-allow-origin" not in response.headers


async def test_auth_start_is_rate_limited(low_auth_rate_limit):
    # Unknown provider fails fast with 400, but the request still counts against
    # the limit — so the third call in the window is refused with 429 before any
    # work is done. This shields the discovery/DB path from being hammered.
    async with _client() as client:
        first = await client.get("/auth/start", params=UNKNOWN_PROVIDER)
        second = await client.get("/auth/start", params=UNKNOWN_PROVIDER)
        third = await client.get("/auth/start", params=UNKNOWN_PROVIDER)

    assert first.status_code == 400
    assert second.status_code == 400
    assert third.status_code == 429


@respx.mock
async def test_a_throttled_route_answers_a_model_until_it_refuses(
    low_auth_rate_limit, tmp_path
):
    """A limited route answering a Pydantic model succeeds, then refuses in shape.

    Two things that only break together. slowapi hands the endpoint's return
    value to its header injection and branches on whether it is already a
    ``Response``, reaching for a ``response`` argument on the endpoint when it is
    not — so a route answering a model fails where one answering ``JSONResponse``
    does not (see ``deps.rate_limit_exceeded_handler`` for why that decides how
    ``Retry-After`` is set). And the refusal itself must read like every other
    refusal here: ``{"detail": …}``, not slowapi's own ``{"error": …}``.

    Driven against `/auth/connect` rather than the deprecated `/auth/start`,
    which keeps its original ``{"error": …}`` body and so is the one route that
    cannot demonstrate the convention.
    """
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'throttle.db'}"):
        async with _client() as client:
            mock_server(SMART_LAUNCHER, token_response("throttle-patient"))
            body = {
                "provider": SMART_LAUNCHER["provider"],
                "iss": SMART_LAUNCHER["iss"],
            }
            first = await client.post("/auth/connect", json=body)
            second = await client.post("/auth/connect", json=body)
            third = await client.post("/auth/connect", json=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    assert third.status_code == 429
    assert "error" not in third.json()
    assert third.json()["detail"].startswith("Rate limit exceeded")
    # The window the limit itself declares, so a caller is told when to return
    # rather than left to guess.
    assert third.headers["retry-after"] == "60"
