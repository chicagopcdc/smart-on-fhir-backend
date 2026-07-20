"""Security posture of the HTTP surface: CORS and rate limiting.

Driven against the live ASGI app so the middleware stack is exercised exactly as
it is in production.
"""

from __future__ import annotations

import pytest

from app import main
from app.core.config import get_settings
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
    main.limiter.enabled = True
    main.limiter.reset()
    yield
    main.limiter.enabled = False
    main.limiter.reset()
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
