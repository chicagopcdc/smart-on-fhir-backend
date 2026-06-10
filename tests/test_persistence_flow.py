"""Persistence flow: state is written at /auth/start, consumed at /auth/callback,
and the exchanged token is stored encrypted.

Drives the live endpoints end-to-end against an on-disk SQLite database with the
SMART token endpoint mocked, then reopens the database with a fresh engine to show
that the token survives a restart and the single-use state was consumed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import main
from db import get_session, persist_token
from models import Base, OAuthState, ProviderToken, utcnow
from providers.models import TokenSet

ISS = "https://fhir.epic.com/interconnect-fhir-oauth"
TOKEN_URL = ISS + "/oauth2/token"


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
    tmp_path, epic_token_response
):
    url = f"sqlite+aiosqlite:///{tmp_path / 'flow.db'}"
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=epic_token_response)
    )

    async with _app_db(url) as factory:
        async with _client() as client:
            state = await _start(client)

            # State lives in the database, not an in-memory dict, so it outlives a restart.
            async with factory() as session:
                assert await session.get(OAuthState, state) is not None

            callback = await client.post(
                "/auth/callback", json={"code": "auth-code-123", "state": state}
            )
            assert callback.status_code == 200
            assert callback.json() == {"success": True}

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
            response = await client.post(
                "/auth/callback", json={"code": "code", "state": "expired"}
            )
            assert response.status_code == 400

        async with factory() as session:
            assert (await session.execute(select(ProviderToken))).first() is None


@respx.mock
async def test_callback_with_failed_exchange_consumes_state_and_stores_no_token(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'fail.db'}"
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
async def test_resources_can_be_fetched_after_the_callback(tmp_path, epic_token_response):
    url = f"sqlite+aiosqlite:///{tmp_path / 'resources.db'}"
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=epic_token_response)
    )
    # Catch-all for the fan-out across resource types on the FHIR base URL.
    fhir_route = respx.route(
        method="GET", url__startswith=ISS + "/api/FHIR/R4/"
    ).mock(
        return_value=httpx.Response(200, json={"resourceType": "Bundle", "entry": []})
    )

    async with _app_db(url):
        async with _client() as client:
            state = await _start(client)
            await client.post(
                "/auth/callback", json={"code": "auth-code-123", "state": state}
            )

            # The state is consumed by now; the stored token row is what routes
            # the patient to their provider.
            response = await client.get(
                "/fhir_resources",
                params={
                    "access_token": epic_token_response["access_token"],
                    "fhir_patient_id": epic_token_response["patient"],
                },
            )

    assert response.status_code == 200
    assert fhir_route.called
    body = response.json()
    assert body["Patient"] == {"resourceType": "Bundle", "entry": []}


async def test_resources_unknown_patient_returns_404(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'nopatient.db'}"

    async with _app_db(url):
        async with _client() as client:
            response = await client.get(
                "/fhir_resources",
                params={"access_token": "tok", "fhir_patient_id": "nobody"},
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
