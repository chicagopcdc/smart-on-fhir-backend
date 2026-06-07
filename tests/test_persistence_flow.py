"""Persistence flow: state is written at /auth/start and an encrypted token at /auth/callback.

Drives the two live endpoints end-to-end against an on-disk SQLite database with
the SMART token endpoint mocked, then reopens the database with a fresh engine to
prove the state and token survive a process restart (they are in Postgres/SQLite,
not the old in-memory dict).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import crypto
import main
from db import get_session
from models import Base, OAuthState, ProviderToken

ISS = "https://fhir.epic.com/interconnect-fhir-oauth"
TOKEN_URL = ISS + "/oauth2/token"


@respx.mock
async def test_auth_flow_persists_state_and_encrypted_token(tmp_path, epic_token_response):
    url = f"sqlite+aiosqlite:///{tmp_path / 'flow.db'}"

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = override_get_session
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=epic_token_response)
    )

    transport = httpx.ASGITransport(app=main.app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            start = await client.get(
                "/auth/start",
                params={"provider": "EPIC_SANDBOX", "iss": ISS},
                follow_redirects=False,
            )
            assert start.status_code == 307
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

            callback = await client.post(
                "/auth/callback", json={"code": "auth-code-123", "state": state}
            )
            assert callback.status_code == 200
            assert callback.json() == {"success": True}
    finally:
        main.app.dependency_overrides.clear()
    await engine.dispose()

    # Reopen with a fresh engine: state and token persisted to disk, not memory.
    engine2 = create_async_engine(url)
    factory2 = async_sessionmaker(engine2, expire_on_commit=False)
    async with factory2() as session:
        state_row = await session.get(OAuthState, state)
        assert state_row is not None
        assert state_row.iss == ISS and state_row.provider == "EPIC_SANDBOX"

        token = (await session.execute(select(ProviderToken))).scalar_one()
        assert token.patient_fhir_id == epic_token_response["patient"]
        assert token.scope == epic_token_response["scope"]

        # The stored column is ciphertext; the application decrypts on read.
        raw = (
            await session.execute(
                text("SELECT access_token FROM provider_token WHERE id = :id"),
                {"id": token.id},
            )
        ).scalar_one()
        assert raw != epic_token_response["access_token"]
        assert raw.startswith("gAAAAA")
        assert crypto.decrypt(raw) == epic_token_response["access_token"]
        assert token.access_token == epic_token_response["access_token"]
    await engine2.dispose()
