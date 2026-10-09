"""The rest of the authentication surface: advertising it, and turning it off.

The flow itself is covered in ``test_login_flow.py`` and what it is *for* in
``test_authenticated_records.py``. These cover the edges around them — what a
frontend is told before anyone signs in, what happens in a deployment that
configured no identity provider, and the two routes that read a session rather
than create one.
"""

from __future__ import annotations

from datetime import timedelta

import respx
from sqlalchemy import select

from app.auth.models import AppSession, ProviderToken, User, utcnow
from app.core.config import get_settings
from app.core.db import delete_dead_connections
from tests import idp
from tests.app_harness import (
    EPIC_SANDBOX,
    app_db,
    bearer,
    client,
    connect,
    refusal,
    token_response,
)
from tests.test_login_flow import sign_in

DB = "sqlite+aiosqlite:///{path}/db.sqlite"


# --- what a frontend is told before anyone signs in ---------------------------


async def test_auth_providers_reports_a_configured_login(tmp_path):
    async with app_db(DB.format(path=tmp_path)):
        async with client() as http:
            response = await http.get("/auth/providers")

    assert response.status_code == 200
    assert response.json() == {
        "loginEnabled": True,
        "loginRequired": False,
        "issuer": idp.ISSUER,
    }


async def test_auth_providers_names_no_issuer_when_login_is_off(
    tmp_path, monkeypatch
):
    """A half-configured deployment must not advertise a sign-in that cannot work."""
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    get_settings.cache_clear()
    try:
        async with app_db(DB.format(path=tmp_path)):
            async with client() as http:
                response = await http.get("/auth/providers")
    finally:
        get_settings.cache_clear()

    assert response.json() == {
        "loginEnabled": False,
        "loginRequired": False,
        "issuer": None,
    }


# --- a deployment with no identity provider -----------------------------------


async def test_login_is_unavailable_rather_than_broken_without_an_issuer(
    tmp_path, monkeypatch
):
    """503 says "this deployment does not offer signing in", which is the truth."""
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    get_settings.cache_clear()
    try:
        async with app_db(DB.format(path=tmp_path)):
            async with client() as http:
                start = await http.post("/auth/login")
                finish = await http.post(
                    "/auth/login/callback", json={"code": "c", "state": "s"}
                )
    finally:
        get_settings.cache_clear()

    assert start.status_code == 503
    assert finish.status_code == 503
    code, message = refusal(start)
    assert code == "LOGIN_NOT_CONFIGURED"
    assert "not configured" in message


@respx.mock
async def test_connecting_still_works_without_an_identity_provider(
    tmp_path, monkeypatch
):
    """Login is an added layer, not a prerequisite. The old path is untouched."""
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    get_settings.cache_clear()
    try:
        async with app_db(DB.format(path=tmp_path)):
            async with client() as http:
                body = await connect(
                    http, EPIC_SANDBOX, token_response("epic-patient")
                )
    finally:
        get_settings.cache_clear()

    assert body["patientId"].startswith("pat_")


# --- reading a session --------------------------------------------------------


async def test_me_requires_a_bearer(tmp_path):
    async with app_db(DB.format(path=tmp_path)):
        async with client() as http:
            missing = await http.get("/auth/me")
            unknown = await http.get("/auth/me", headers=bearer("not-a-session"))

    assert missing.status_code == 401
    assert unknown.status_code == 401


async def test_logout_requires_a_bearer(tmp_path):
    async with app_db(DB.format(path=tmp_path)):
        async with client() as http:
            response = await http.post("/auth/logout")

    assert response.status_code == 401


@respx.mock
async def test_me_reports_the_remaining_life_of_the_session(tmp_path, monkeypatch):
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            response = await http.get("/auth/me", headers=bearer(login["sessionId"]))

    ttl = get_settings().app_session_ttl_seconds
    # Counted down from the TTL rather than restated as it, so a frontend can see
    # a session ageing.
    assert 0 < response.json()["expiresIn"] <= ttl


@respx.mock
async def test_logging_out_only_ends_the_session_presented(tmp_path, monkeypatch):
    """Signing out on one device leaves the others signed in."""
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            phone = await sign_in(http, factory, monkeypatch)
            laptop = await sign_in(http, factory, monkeypatch)

            idp.mock_discovery()
            await http.post("/auth/logout", headers=bearer(phone["sessionId"]))

            gone = await http.get("/auth/me", headers=bearer(phone["sessionId"]))
            still_here = await http.get("/auth/me", headers=bearer(laptop["sessionId"]))

    assert gone.status_code == 401
    assert still_here.status_code == 200


@respx.mock
async def test_logout_succeeds_even_when_the_provider_cannot_be_reached(
    tmp_path, monkeypatch
):
    """The session is already gone; an IdP having a bad day cannot undo that."""
    from app.auth import oidc

    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            # Dropped so the logout actually reaches for the configuration rather
            # than being served the one the sign-in cached a moment ago.
            oidc.client().clear_cache()
            idp.mock_discovery(status=500)
            response = await http.post(
                "/auth/logout", headers=bearer(login["sessionId"])
            )
            after = await http.get("/auth/me", headers=bearer(login["sessionId"]))

    assert response.status_code == 200
    assert response.json() == {"success": True, "logoutUrl": None}
    assert after.status_code == 401


@respx.mock
async def test_the_deprecated_read_route_has_nothing_to_read_for_a_login_session(
    tmp_path, monkeypatch
):
    """It reads "the connection this session was issued for"; a login has none."""
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            response = await http.get(
                "/fhir_resources", headers=bearer(login["sessionId"])
            )

    assert response.status_code == 404
    assert refusal(response) == (
        "CONNECTION_NOT_FOUND",
        "No connected provider for patient",
    )


# --- the sweep has to see an account's session --------------------------------


@respx.mock
async def test_a_signed_in_users_connection_is_not_swept(tmp_path, monkeypatch):
    """A login session carries no patient_id, so the sweep has to find it via the account.

    Without that, every connection an account owns would look unreachable the
    moment it was made, and the first sweep would delete a connection somebody
    was actively using.
    """
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            await connect(
                http,
                EPIC_SANDBOX,
                # No refresh token, so nothing but reachability can save it.
                token_response("epic-patient", refresh_token=None, expires_in=1),
                link_session=login["sessionId"],
            )

        async with factory() as session:
            # Age the access token past its life, and drop every session but the
            # one the login minted — which is the state a caller who signed in
            # and then let the connect session lapse is left in.
            await session.execute(
                ProviderToken.__table__.update().values(
                    expires_at=utcnow() - timedelta(hours=1)
                )
            )
            await session.execute(
                AppSession.__table__.delete().where(AppSession.user_id.is_(None))
            )
            await session.execute(
                AppSession.__table__.delete().where(
                    AppSession.session_id != login["sessionId"]
                )
            )
            await session.commit()

            removed = await delete_dead_connections(session)
            await session.commit()
            left = (await session.execute(select(ProviderToken))).scalars().all()
            user = (await session.execute(select(User))).scalar_one()

    assert removed == 0
    assert len(left) == 1
    assert user.patient_id == left[0].patient_id
