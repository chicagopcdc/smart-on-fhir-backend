"""Auth flow end to end: discover, authorize, exchange, persist, then fetch.

Drives the live endpoints against an on-disk SQLite database with the server's
SMART discovery document and token endpoint mocked. Shows the redirect is built
from discovery (aud derived from the issuer, PKCE attached), the exchange uses
the auth method the server advertises, the token is stored encrypted, and FHIR
resources are then fetched with that stored token — never one passed in the URL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.core.db import get_session, persist_token
from app.auth.models import Base, OAuthState, ProviderToken, utcnow
from app.providers.models import TokenSet

# The issuer is the FHIR base URL; discovery, aud, and resource calls all use it.
ISS = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
WELL_KNOWN_URL = ISS + "/.well-known/smart-configuration"
# Endpoints the discovery document advertises (absolute, at the OAuth base).
AUTHORIZE_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize"
TOKEN_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token"


@pytest.fixture(autouse=True)
def _reset_discovery_cache():
    """Keep the shared discovery cache from leaking configs between tests."""
    main._discovery.clear()
    yield
    main._discovery.clear()


@asynccontextmanager
async def _app_db(url: str):
    """Bind the app to a throwaway database for the duration of a test."""
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = override_get_session
    try:
        yield factory
    finally:
        main.app.dependency_overrides.clear()
        await engine.dispose()


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )


async def _start(client) -> str:
    response = await client.get(
        "/auth/start",
        params={"provider": "EPIC_SANDBOX", "iss": ISS},
        follow_redirects=False,
    )
    assert response.status_code == 307
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


@respx.mock
async def test_auth_flow_persists_state_then_consumes_it_and_stores_token(
    tmp_path, epic_smart_config, epic_token_response
):
    url = f"sqlite+aiosqlite:///{tmp_path / 'flow.db'}"
    respx.get(WELL_KNOWN_URL).mock(
        return_value=httpx.Response(200, json=epic_smart_config)
    )
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=epic_token_response)
    )

    async with _app_db(url) as factory:
        async with _client() as client:
            response = await client.get(
                "/auth/start",
                params={"provider": "EPIC_SANDBOX", "iss": ISS},
                follow_redirects=False,
            )
            assert response.status_code == 307
            location = response.headers["location"]
            query = parse_qs(urlparse(location).query)
            state = query["state"][0]

            # The redirect is built from discovery: rooted at the discovered
            # endpoint, aud derived from the issuer, PKCE attached.
            assert location.startswith(AUTHORIZE_URL)
            assert query["aud"] == [ISS]
            assert "code_challenge" in query

            # State lives in the database with its PKCE verifier, outliving a restart.
            async with factory() as session:
                row = await session.get(OAuthState, state)
                assert row is not None
                assert row.code_verifier is not None

            callback = await client.post(
                "/auth/callback", json={"code": "auth-code-123", "state": state}
            )
            assert callback.status_code == 200
            assert callback.json() == {
                "success": True,
                "patient": epic_token_response["patient"],
            }

    # The exchange used Basic auth (chosen from discovery) and replayed the verifier.
    exchange = token_route.calls.last.request
    assert exchange.headers["Authorization"].startswith("Basic ")
    assert "code_verifier" in parse_qs(exchange.content.decode())

    # Reopen with a fresh engine: the consumed state is gone, the token persisted.
    engine = create_async_engine(url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        assert await session.get(OAuthState, state) is None  # single-use

        token = (await session.execute(select(ProviderToken))).scalar_one()
        assert token.patient_fhir_id == epic_token_response["patient"]
        assert token.scope == epic_token_response["scope"]
        # Decrypts transparently on read, end to end across a restart.
        assert token.access_token == epic_token_response["access_token"]
    await engine.dispose()


@respx.mock
async def test_start_rejects_issuer_not_allowed_for_provider(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'badiss.db'}"

    async with _app_db(url) as factory:
        async with _client() as client:
            # An issuer the provider was never registered with: rejected before
            # any discovery request, so no outbound call is made (respx would
            # raise on one) and no state is written.
            response = await client.get(
                "/auth/start",
                params={"provider": "EPIC_SANDBOX", "iss": "https://evil.example/fhir"},
                follow_redirects=False,
            )
            assert response.status_code == 400

        async with factory() as session:
            assert (await session.execute(select(OAuthState))).first() is None


async def test_callback_rejects_expired_state(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'expired.db'}"

    async with _app_db(url) as factory:
        async with factory() as session:
            session.add(
                OAuthState(
                    state="expired",
                    iss=ISS,
                    provider="EPIC_SANDBOX",
                    expires_at=utcnow() - timedelta(seconds=1),
                )
            )
            await session.commit()

        async with _client() as client:
            # Rejected on the state check, before any discovery or exchange.
            response = await client.post(
                "/auth/callback", json={"code": "code", "state": "expired"}
            )
            assert response.status_code == 400

        async with factory() as session:
            assert (await session.execute(select(ProviderToken))).first() is None


@respx.mock
async def test_callback_with_failed_exchange_consumes_state_and_stores_no_token(
    tmp_path, epic_smart_config
):
    url = f"sqlite+aiosqlite:///{tmp_path / 'fail.db'}"
    respx.get(WELL_KNOWN_URL).mock(
        return_value=httpx.Response(200, json=epic_smart_config)
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )

    async with _app_db(url) as factory:
        async with _client() as client:
            state = await _start(client)
            response = await client.post(
                "/auth/callback", json={"code": "bad-code", "state": state}
            )
            assert response.status_code == 400

        async with factory() as session:
            assert await session.get(OAuthState, state) is None  # consumed even on failure
            assert (await session.execute(select(ProviderToken))).first() is None


@respx.mock
async def test_resources_are_fetched_with_the_stored_token(
    tmp_path, epic_smart_config, epic_token_response
):
    url = f"sqlite+aiosqlite:///{tmp_path / 'resources.db'}"
    respx.get(WELL_KNOWN_URL).mock(
        return_value=httpx.Response(200, json=epic_smart_config)
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=epic_token_response)
    )
    # Catch-all for the fan-out across resource types on the FHIR base URL. The
    # well-known route is registered first, so it still wins for that path.
    fhir_route = respx.route(method="GET", url__startswith=ISS + "/").mock(
        return_value=httpx.Response(200, json={"resourceType": "Bundle", "entry": []})
    )

    async with _app_db(url):
        async with _client() as client:
            state = await _start(client)
            await client.post(
                "/auth/callback", json={"code": "auth-code-123", "state": state}
            )

            # No access_token in the request: the stored token routes the patient.
            response = await client.get(
                "/fhir_resources",
                params={"fhir_patient_id": epic_token_response["patient"]},
            )

    assert response.status_code == 200
    assert fhir_route.called
    # The FHIR call carries the stored token as a Bearer header, not in the URL.
    fhir_request = fhir_route.calls.last.request
    assert (
        fhir_request.headers["Authorization"]
        == f"Bearer {epic_token_response['access_token']}"
    )
    assert epic_token_response["access_token"] not in str(fhir_request.url)
    assert response.json()["Patient"] == {"resourceType": "Bundle", "entry": []}


async def test_resources_unknown_patient_returns_404(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'nopatient.db'}"

    async with _app_db(url):
        async with _client() as client:
            response = await client.get(
                "/fhir_resources", params={"fhir_patient_id": "nobody"}
            )

    assert response.status_code == 404


async def test_persist_token_updates_the_existing_identity(db_session):
    identity = {"provider": "EPIC_SANDBOX", "iss": ISS}
    await persist_token(
        db_session,
        **identity,
        token_set=TokenSet(access_token="first", patient="p1", scope="scope-1"),
    )
    await persist_token(
        db_session,
        **identity,
        token_set=TokenSet(access_token="second", patient="p1", scope="scope-2"),
    )

    rows = (await db_session.execute(select(ProviderToken))).scalars().all()
    assert len(rows) == 1
    assert rows[0].access_token == "second"
    assert rows[0].scope == "scope-2"
