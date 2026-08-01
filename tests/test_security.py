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
    params = {"provider": "NOPE", "iss": "https://example.org/fhir"}
    async with _client() as client:
        first = await client.get("/auth/start", params=params)
        second = await client.get("/auth/start", params=params)
        third = await client.get("/auth/start", params=params)

    assert first.status_code == 400
    assert second.status_code == 400
    assert third.status_code == 429


@respx.mock
async def test_the_throttle_leaves_a_route_that_returns_a_model_alone(
    low_auth_rate_limit, tmp_path
):
    """A limited route that answers with a Pydantic model still succeeds.

    slowapi hands the endpoint's return value to its header injection, and takes
    a different branch when that value is not already a ``Response``: it reaches
    for a ``response`` argument on the endpoint instead, and raises when there is
    none. So the routes that answer with a model — which is most of them now —
    fail in a way the ones answering ``JSONResponse`` never do. Worth pinning,
    because the difference is invisible until the limiter is switched on.
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


async def test_a_throttled_request_is_refused_like_every_other_refusal(
    low_auth_rate_limit,
):
    """429 answers `detail`, not slowapi's own `error`.

    Every other refusal in this API is an ``HTTPException``, so it arrives as
    ``{"detail": …}``. Leaving the throttle to answer ``{"error": …}`` would make
    it the one status a client has to special-case, and ``Retry-After`` is what
    turns a refusal into something a client can act on rather than guess at.
    """
    params = {"provider": "NOPE", "iss": "https://example.org/fhir"}
    async with _client() as client:
        for _ in range(3):
            response = await client.get("/auth/start", params=params)

    assert response.status_code == 429
    assert "error" not in response.json()
    assert response.json()["detail"].startswith("Rate limit exceeded")
    assert response.headers["retry-after"] == "60"
