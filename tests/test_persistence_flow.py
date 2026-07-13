"""Auth flow end to end: discover, authorize, exchange, persist, then fetch.

Drives the live endpoints against an on-disk SQLite database with the server's
SMART discovery document and token endpoint mocked. Shows the redirect is built
from discovery (aud derived from the issuer, PKCE attached), the exchange uses
the auth method the server advertises, the token is stored encrypted, and FHIR
resources are then fetched with that stored token — never one passed in the URL.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import get_session, persist_token
from app.auth.models import AppSession, OAuthState, ProviderToken, utcnow
from app.providers import config
from app.providers.models import TokenSet
from tests.app_harness import app_db as _app_db, client as _client

# The issuer is the FHIR base URL; discovery, aud, and resource calls all use it.
ISS = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
WELL_KNOWN_URL = ISS + "/.well-known/smart-configuration"
# Endpoints the discovery document advertises (absolute, at the OAuth base).
AUTHORIZE_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize"
TOKEN_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token"


async def _start(client) -> str:
    response = await client.get(
        "/auth/start",
        params={"provider": "EPIC_SANDBOX", "iss": ISS},
        follow_redirects=False,
    )
    assert response.status_code == 307
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


async def _authorize(client) -> str:
    """Run start + callback and return the issued app session bearer."""
    state = await _start(client)
    callback = await client.post(
        "/auth/callback", json={"code": "auth-code-123", "state": state}
    )
    return callback.json()["session_id"]


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
            body = callback.json()
            assert body["success"] is True
            assert body["patient"] == epic_token_response["patient"]
            # A session bearer is returned for the frontend to read resources with.
            assert body["session_id"]

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


@respx.mock
async def test_start_on_unconfigured_provider_returns_503(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'unconfigured.db'}"
    # Simulate a deployment that never set the provider's client_id: we should
    # fail clearly rather than redirect the user to the EHR with an empty one.
    ehr = dict(config.EHR_CONFIGS["EPIC_SANDBOX"], client_id=None)
    monkeypatch.setitem(config.EHR_CONFIGS, "EPIC_SANDBOX", ehr)

    async with _app_db(url) as factory:
        async with _client() as client:
            response = await client.get(
                "/auth/start",
                params={"provider": "EPIC_SANDBOX", "iss": ISS},
                follow_redirects=False,
            )
            assert response.status_code == 503

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
async def test_callback_rejects_a_token_without_a_patient(tmp_path, epic_smart_config):
    url = f"sqlite+aiosqlite:///{tmp_path / 'nopatient_token.db'}"
    respx.get(WELL_KNOWN_URL).mock(
        return_value=httpx.Response(200, json=epic_smart_config)
    )
    # A token response with no patient context can't anchor a patient session;
    # storing it under an empty id would let two such sessions collapse together.
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "scope": "s"})
    )

    async with _app_db(url) as factory:
        async with _client() as client:
            state = await _start(client)
            response = await client.post(
                "/auth/callback", json={"code": "auth-code-123", "state": state}
            )
            assert response.status_code == 400

        # Nothing is persisted: no token row and no session.
        async with factory() as session:
            assert (await session.execute(select(ProviderToken))).first() is None
            assert (await session.execute(select(AppSession))).first() is None


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
            session_id = await _authorize(client)

            # The session — not a caller-supplied patient id — selects the patient
            # and their stored token; no access_token is passed in the request.
            response = await client.get(
                "/fhir_resources",
                headers={"Authorization": f"Bearer {session_id}"},
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


async def test_resources_without_a_session_are_refused(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'nosession.db'}"

    async with _app_db(url):
        async with _client() as client:
            # No Authorization header: the caller cannot name a patient to read.
            no_header = await client.get("/fhir_resources")
            # A bearer that matches no session is equally rejected.
            bad_bearer = await client.get(
                "/fhir_resources", headers={"Authorization": "Bearer not-a-session"}
            )

    assert no_header.status_code == 401
    assert bad_bearer.status_code == 401


async def test_resources_reject_an_expired_session(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'expiredsession.db'}"

    async with _app_db(url) as factory:
        async with factory() as session:
            session.add(
                AppSession(
                    session_id="expired-session",
                    patient_fhir_id="p1",
                    provider="EPIC_SANDBOX",
                    iss=ISS,
                    expires_at=utcnow() - timedelta(seconds=1),
                )
            )
            await session.commit()

        async with _client() as client:
            response = await client.get(
                "/fhir_resources",
                headers={"Authorization": "Bearer expired-session"},
            )

    assert response.status_code == 401


@respx.mock
async def test_a_session_reads_only_its_own_patient(tmp_path, epic_smart_config):
    url = f"sqlite+aiosqlite:///{tmp_path / 'crosstenant.db'}"
    respx.get(WELL_KNOWN_URL).mock(
        return_value=httpx.Response(200, json=epic_smart_config)
    )
    # Two authorizations against the same provider return two different patients,
    # each with its own access token.
    respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(
                200, json={"access_token": "token-A", "patient": "patient-A", "scope": "s"}
            ),
            httpx.Response(
                200, json={"access_token": "token-B", "patient": "patient-B", "scope": "s"}
            ),
        ]
    )
    fhir_route = respx.route(method="GET", url__startswith=ISS + "/").mock(
        return_value=httpx.Response(200, json={"resourceType": "Bundle", "entry": []})
    )

    async with _app_db(url):
        async with _client() as client:
            session_a = await _authorize(client)  # bound to patient-A
            await _authorize(client)  # bound to patient-B, most recently stored

        async with _client() as client:
            await client.get(
                "/fhir_resources", headers={"Authorization": f"Bearer {session_a}"}
            )

    # Every resource call for session A must carry patient A's token — never the
    # more recently stored patient B's. This is the IDOR guarantee: the session,
    # not recency or a query param, decides whose data is read.
    assert fhir_route.called
    used_tokens = {
        call.request.headers["Authorization"] for call in fhir_route.calls
    }
    assert used_tokens == {"Bearer token-A"}


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
