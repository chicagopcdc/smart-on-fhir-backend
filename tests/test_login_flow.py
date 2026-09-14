"""Signing in end to end, and every way a sign-in is refused.

Drives the real routes against the fake identity provider in ``tests/idp.py``,
so the ID token these tests hand back is genuinely signed and genuinely
verified. What is asserted throughout is not that a session comes back, but
*which* one: a bearer that names an account, and an account that names the
record its providers hang off.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import respx
from sqlalchemy import select

from app.auth.models import AppSession, LoginState, User
from app.core.config import get_settings
from tests import idp
from tests.app_harness import app_db, client


async def start_login(http) -> dict:
    """Begin a login and return the response body, having mocked discovery."""
    idp.mock_discovery()
    response = await http.post("/auth/login")
    assert response.status_code == 200, response.text
    return response.json()


async def nonce_for(factory, state: str) -> str:
    """The nonce the server minted for this login, as the IdP would echo it."""
    async with factory() as session:
        row = await session.get(LoginState, state)
        assert row is not None
        return row.nonce


async def sign_in(http, factory, monkeypatch, **token_kwargs) -> dict:
    """Run a whole login against the fake IdP and return the callback body."""
    started = await start_login(http)
    nonce = await nonce_for(factory, started["state"])
    idp.mock_token_endpoint(
        idp.token_response(idp.id_token(nonce=nonce, **token_kwargs))
    )
    with idp.serving_keys(monkeypatch):
        response = await http.post(
            "/auth/login/callback",
            json={"code": "test-login-code", "state": started["state"]},
        )
    assert response.status_code == 200, response.text
    return response.json()


# --- starting a login ---------------------------------------------------------


@respx.mock
async def test_login_returns_the_providers_authorization_url(tmp_path):
    """The URL is built from what the provider publishes, not from a template."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite"):
        async with client() as http:
            body = await start_login(http)

    url = urlparse(body["authorizationUrl"])
    query = parse_qs(url.query)

    assert f"{url.scheme}://{url.netloc}{url.path}" == idp.AUTHORIZATION_ENDPOINT
    assert query["client_id"] == [idp.CLIENT_ID]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid profile email"]
    assert query["state"] == [body["state"]]
    assert query["redirect_uri"] == ["http://localhost:3000/auth/login/callback"]
    # PKCE is always sent to the one issuer this deployment chose.
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"]
    # The nonce is what binds the ID token to this request; it must not be the
    # state, which travels in the open and comes back in a URL.
    assert query["nonce"] and query["nonce"] != [body["state"]]


@respx.mock
async def test_login_persists_the_state_with_its_secrets(tmp_path):
    """The verifier and nonce are kept server-side, never sent to the browser."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            body = await start_login(http)

        async with factory() as session:
            row = await session.get(LoginState, body["state"])

    assert row is not None
    assert row.code_verifier and row.nonce
    assert row.code_verifier not in body["authorizationUrl"]
    assert row.nonce != row.code_verifier


@respx.mock
async def test_login_reports_a_provider_it_cannot_read(tmp_path):
    """An unreachable IdP is an upstream fault, not the user's."""
    idp.mock_discovery(status=500)
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite"):
        async with client() as http:
            response = await http.post("/auth/login")

    assert response.status_code == 502
    assert "identity provider" in response.json()["detail"]


@respx.mock
async def test_login_refuses_a_document_published_by_another_issuer(tmp_path):
    """A discovery document that names a different issuer is someone else's."""
    idp.mock_discovery(idp.discovery_document(issuer="https://elsewhere.test"))
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite"):
        async with client() as http:
            response = await http.post("/auth/login")

    assert response.status_code == 502


# --- completing one -----------------------------------------------------------


@respx.mock
async def test_sign_in_creates_an_account_and_a_session(tmp_path, monkeypatch):
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            body = await sign_in(http, factory, monkeypatch)

        async with factory() as session:
            users = (await session.execute(select(User))).scalars().all()
            app_session = await session.get(AppSession, body["sessionId"])

    assert body["user"]["email"] == "avery@example.org"
    assert body["user"]["displayName"] == "Avery Diaz"
    # No provider connected yet, so the account owns no record. Null rather than
    # an invented id: there is nothing to read.
    assert body["user"]["patientId"] is None
    assert body["expiresIn"] == get_settings().app_session_ttl_seconds

    assert len(users) == 1
    assert users[0].issuer == idp.ISSUER
    assert users[0].subject == "00u1a2b3c4d5e6f7g8h9"
    assert users[0].id.startswith("usr_")
    # The account's id is ours, never the provider's subject.
    assert users[0].subject not in users[0].id

    # The session names the account and nothing else: no record, no connection.
    assert app_session is not None
    assert app_session.user_id == users[0].id
    assert app_session.patient_id is None
    assert app_session.provider is None


@respx.mock
async def test_signing_in_twice_returns_to_the_same_account(tmp_path, monkeypatch):
    """A second sign-in finds the account rather than starting another."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            first = await sign_in(http, factory, monkeypatch)
            second = await sign_in(http, factory, monkeypatch)

        async with factory() as session:
            users = (await session.execute(select(User))).scalars().all()

    assert len(users) == 1
    assert first["user"]["userId"] == second["user"]["userId"]
    # A new session each time, so signing in on a second device does not
    # invalidate the first.
    assert first["sessionId"] != second["sessionId"]


@respx.mock
async def test_the_account_follows_the_subject_not_the_email(tmp_path, monkeypatch):
    """A changed email is an update to the account, not a different person."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            first = await sign_in(http, factory, monkeypatch)
            second = await sign_in(
                http, factory, monkeypatch, email="avery.diaz@example.org"
            )

        async with factory() as session:
            users = (await session.execute(select(User))).scalars().all()

    assert first["user"]["userId"] == second["user"]["userId"]
    assert len(users) == 1
    # And the row carries what the provider last said, not what it first said.
    assert users[0].email == "avery.diaz@example.org"


@respx.mock
async def test_two_subjects_are_two_accounts(tmp_path, monkeypatch):
    """Even sharing an address, which is why the address cannot be the key."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            first = await sign_in(http, factory, monkeypatch, subject="subject-one")
            second = await sign_in(http, factory, monkeypatch, subject="subject-two")

    assert first["user"]["userId"] != second["user"]["userId"]


@respx.mock
async def test_the_state_is_consumed_so_a_login_cannot_be_replayed(
    tmp_path, monkeypatch
):
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            started = await start_login(http)
            nonce = await nonce_for(factory, started["state"])
            idp.mock_token_endpoint(idp.token_response(idp.id_token(nonce=nonce)))

            with idp.serving_keys(monkeypatch):
                first = await http.post(
                    "/auth/login/callback",
                    json={"code": "test-login-code", "state": started["state"]},
                )
                replay = await http.post(
                    "/auth/login/callback",
                    json={"code": "test-login-code", "state": started["state"]},
                )

    assert first.status_code == 200
    assert replay.status_code == 400
    assert replay.json()["detail"] == "Invalid or expired state"


@respx.mock
async def test_an_unknown_state_is_refused(tmp_path, monkeypatch):
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite"):
        async with client() as http:
            with idp.serving_keys(monkeypatch):
                response = await http.post(
                    "/auth/login/callback",
                    json={"code": "test-login-code", "state": "never-issued"},
                )

    assert response.status_code == 400


@respx.mock
async def test_the_exchange_sends_the_stored_verifier(tmp_path, monkeypatch):
    """PKCE only proves anything if the verifier actually leaves the building."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            started = await start_login(http)
            async with factory() as session:
                state_row = await session.get(LoginState, started["state"])
                verifier, nonce = state_row.code_verifier, state_row.nonce

            route = idp.mock_token_endpoint(
                idp.token_response(idp.id_token(nonce=nonce))
            )
            with idp.serving_keys(monkeypatch):
                await http.post(
                    "/auth/login/callback",
                    json={"code": "test-login-code", "state": started["state"]},
                )

    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["code_verifier"] == [verifier]
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["test-login-code"]


@respx.mock
async def test_a_refused_exchange_is_reported_as_upstream(tmp_path, monkeypatch):
    """Our state was good, so what failed is between us and the provider."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            started = await start_login(http)
            idp.mock_token_endpoint({"error": "invalid_grant"}, status=400)
            with idp.serving_keys(monkeypatch):
                response = await http.post(
                    "/auth/login/callback",
                    json={"code": "stale-code", "state": started["state"]},
                )

    assert response.status_code == 502


# --- the ID token is where a sign-in is actually decided ----------------------


@pytest.mark.parametrize(
    "claims, because",
    [
        ({"key": idp.impostor_key, "kid": idp.IMPOSTOR_KEY_ID}, "signed by a key the provider does not publish"),
        ({"audience": "some-other-client"}, "issued for a different application"),
        ({"issuer": "https://elsewhere.test"}, "issued by a different provider"),
        ({"expires_in": -60}, "already expired"),
        ({"subject": None}, "carries no subject"),
    ],
)
@respx.mock
async def test_an_id_token_that_does_not_verify_is_refused(
    tmp_path, monkeypatch, claims, because
):
    """Each of these is the only thing standing between an assertion and an account."""
    if callable(claims.get("key")):
        claims = {**claims, "key": claims["key"]()}

    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            started = await start_login(http)
            nonce = await nonce_for(factory, started["state"])
            idp.mock_token_endpoint(
                idp.token_response(idp.id_token(nonce=nonce, **claims))
            )
            with idp.serving_keys(monkeypatch):
                response = await http.post(
                    "/auth/login/callback",
                    json={"code": "test-login-code", "state": started["state"]},
                )

        async with factory() as session:
            users = (await session.execute(select(User))).scalars().all()

    assert response.status_code == 400, f"a token {because} was accepted"
    assert response.json()["detail"] == "Sign-in could not be verified"
    # And nothing was written: a refused sign-in leaves no account behind.
    assert users == []


@respx.mock
async def test_an_id_token_for_another_login_is_refused(tmp_path, monkeypatch):
    """The nonce is what stops a token obtained elsewhere being replayed in."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            started = await start_login(http)
            idp.mock_token_endpoint(
                idp.token_response(idp.id_token(nonce="a-nonce-from-another-login"))
            )
            with idp.serving_keys(monkeypatch):
                response = await http.post(
                    "/auth/login/callback",
                    json={"code": "test-login-code", "state": started["state"]},
                )

    assert response.status_code == 400
    assert response.json()["detail"] == "Sign-in could not be verified"


@respx.mock
async def test_a_token_response_with_no_id_token_is_refused(tmp_path, monkeypatch):
    """An OAuth response is not an OIDC one; without an ID token nobody signed in."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            started = await start_login(http)
            idp.mock_token_endpoint(
                {"access_token": "an-access-token", "token_type": "Bearer"}
            )
            with idp.serving_keys(monkeypatch):
                response = await http.post(
                    "/auth/login/callback",
                    json={"code": "test-login-code", "state": started["state"]},
                )

    assert response.status_code == 400


@respx.mock
async def test_unreadable_signing_keys_are_an_upstream_fault(tmp_path, monkeypatch):
    """Failing to fetch the keys says nothing about the token; do not blame the user."""
    from jwt import PyJWKClient
    from jwt.exceptions import PyJWKClientConnectionError

    async with app_db(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite") as factory:
        async with client() as http:
            started = await start_login(http)
            nonce = await nonce_for(factory, started["state"])
            idp.mock_token_endpoint(idp.token_response(idp.id_token(nonce=nonce)))

            def unreachable(self):
                raise PyJWKClientConnectionError("could not reach the key set")

            monkeypatch.setattr(PyJWKClient, "fetch_data", unreachable)
            response = await http.post(
                "/auth/login/callback",
                json={"code": "test-login-code", "state": started["state"]},
            )

    assert response.status_code == 502
