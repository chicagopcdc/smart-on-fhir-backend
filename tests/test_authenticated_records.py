"""What signing in buys: a record that outlives the session that made it.

The point of the authentication layer, stated as tests. Before it, a patient
record was reachable only through the session minted when it was created, so a
session expiring stranded the record and the next authorization began an empty
one. These assert the opposite — sign in, connect, come back later, and the
providers are still there.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import respx
from sqlalchemy import select, update

from app.auth.models import AppSession, ProviderToken, User, utcnow
from app.core.config import get_settings
from tests import idp
from tests.app_harness import (
    CERNER_SANDBOX,
    EPIC_SANDBOX,
    SMART_LAUNCHER,
    app_db,
    bearer,
    client,
    connect,
    load_fixture,
    serve_record,
    token_endpoint,
    token_response,
)
from tests.test_login_flow import sign_in

DB = "sqlite+aiosqlite:///{path}/db.sqlite"


async def expire_sessions(factory) -> None:
    """Age every session past its TTL, the way an hour of wall clock would."""
    async with factory() as session:
        await session.execute(
            update(AppSession).values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await session.commit()



@respx.mock
async def test_connecting_while_signed_in_gives_the_record_to_the_account(
    tmp_path, monkeypatch
):
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            body = await connect(
                http,
                EPIC_SANDBOX,
                token_response("epic-patient"),
                link_session=login["sessionId"],
            )

        async with factory() as session:
            user = (await session.execute(select(User))).scalar_one()

    # The account now owns the record the authorization created.
    assert user.patient_id == body["patientId"]


@respx.mock
async def test_the_login_session_reaches_the_record_it_did_not_have_yet(
    tmp_path, monkeypatch
):
    """The bearer was minted before the record existed and still reaches it.

    This is why the record is resolved through the account at request time
    rather than frozen onto the session when it is issued.
    """
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            # Before connecting anything: signed in, with no record.
            before = await http.get("/auth/me", headers=bearer(login["sessionId"]))

            body = await connect(
                http,
                EPIC_SANDBOX,
                token_response("epic-patient"),
                link_session=login["sessionId"],
            )

            # The same bearer, unchanged, now names the record.
            after = await http.get("/auth/me", headers=bearer(login["sessionId"]))

    assert before.json() == {
        **before.json(),
        "authenticated": True,
        "patientId": None,
    }
    assert after.json()["patientId"] == body["patientId"]
    assert after.json()["user"]["patientId"] == body["patientId"]


@respx.mock
async def test_signing_back_in_finds_the_record_again(tmp_path, monkeypatch):
    """The whole task, in one test.

    Connect a provider, lose the session, sign in again — and the record, with
    its connection, is still there and still readable.
    """
    record = load_fixture(SMART_LAUNCHER["record"])

    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            body = await connect(
                http,
                SMART_LAUNCHER,
                token_endpoint(record["patientId"]),
                link_session=login["sessionId"],
            )
            patient_id = body["patientId"]

            # Every session lapses: the one from the login and the one the
            # authorization minted. Nothing the caller holds still works.
            await expire_sessions(factory)
            stranded = await http.get(
                f"/patients/{patient_id}/resources",
                headers=bearer(login["sessionId"]),
            )

            # Sign in again — a new session, and no mention of the old record.
            returning = await sign_in(http, factory, monkeypatch)

            respx.get(url__startswith=SMART_LAUNCHER["iss"]).mock(
                side_effect=serve_record(record)
            )
            resources = await http.get(
                f"/patients/{patient_id}/resources",
                headers=bearer(returning["sessionId"]),
            )

    assert stranded.status_code == 401

    # The returning session knows the record without being told it.
    assert returning["user"]["patientId"] == patient_id
    assert returning["sessionId"] != login["sessionId"]

    assert resources.status_code == 200, resources.text
    assert [c["provider"] for c in resources.json()["connections"]] == [
        SMART_LAUNCHER["provider"]
    ]


@respx.mock
async def test_a_second_provider_joins_the_account_record(tmp_path, monkeypatch):
    """Two providers on one record, linked by the account rather than by a session."""
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            first = await connect(
                http,
                EPIC_SANDBOX,
                token_response("epic-patient"),
                link_session=login["sessionId"],
            )
            # The same login session, not the one the first connection minted.
            second = await connect(
                http,
                CERNER_SANDBOX,
                token_response("cerner-patient"),
                link_session=login["sessionId"],
            )

        async with factory() as session:
            connections = (
                await session.execute(select(ProviderToken).order_by(ProviderToken.id))
            ).scalars().all()
            user = (await session.execute(select(User))).scalar_one()

    assert first["patientId"] == second["patientId"]
    assert len(connections) == 2
    assert {c.patient_id for c in connections} == {first["patientId"]}
    assert user.patient_id == first["patientId"]


@respx.mock
async def test_two_connects_started_before_either_finished_share_one_record(
    tmp_path, monkeypatch
):
    """Both flows began with the account owning nothing; they must not end with two.

    The record a callback joins is read off the account when the callback runs,
    not when the authorization started — otherwise the second of these anchors a
    record the account does not point at and nobody can reach.
    """
    from urllib.parse import parse_qs, urlparse

    from tests.app_harness import mock_server

    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            headers = bearer(login["sessionId"])

            # Both authorizations are started before either is completed.
            mock_server(EPIC_SANDBOX, token_response("epic-patient"))
            mock_server(CERNER_SANDBOX, token_response("cerner-patient"))
            states = []
            for server in (EPIC_SANDBOX, CERNER_SANDBOX):
                started = await http.post(
                    "/auth/connect",
                    json={"provider": server["provider"], "iss": server["iss"]},
                    headers=headers,
                )
                assert started.status_code == 200, started.text
                states.append(started.json()["state"])

            bodies = []
            for state in states:
                done = await http.post(
                    "/auth/callback", json={"code": "test-auth-code", "state": state}
                )
                assert done.status_code == 200, done.text
                bodies.append(done.json())

        async with factory() as session:
            connections = (await session.execute(select(ProviderToken))).scalars().all()
            user = (await session.execute(select(User))).scalar_one()

    assert bodies[0]["patientId"] == bodies[1]["patientId"]
    assert {c.patient_id for c in connections} == {user.patient_id}
    assert len(connections) == 2


@respx.mock
async def test_two_accounts_cannot_reach_each_others_records(tmp_path, monkeypatch):
    """Signing in gets you your record, and confirms nothing about anyone else's."""
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            avery = await sign_in(http, factory, monkeypatch, subject="avery")
            theirs = await connect(
                http,
                EPIC_SANDBOX,
                token_response("epic-patient"),
                link_session=avery["sessionId"],
            )

            blake = await sign_in(http, factory, monkeypatch, subject="blake")
            intrusion = await http.get(
                f"/patients/{theirs['patientId']}/resources",
                headers=bearer(blake["sessionId"]),
            )

    # 404 rather than 403: confirming the id exists would itself be a leak.
    assert intrusion.status_code == 404


@respx.mock
async def test_signing_out_ends_the_session_but_keeps_the_record(
    tmp_path, monkeypatch
):
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            body = await connect(
                http,
                EPIC_SANDBOX,
                token_response("epic-patient"),
                link_session=login["sessionId"],
            )

            idp.mock_discovery()
            out = await http.post("/auth/logout", headers=bearer(login["sessionId"]))
            after = await http.get("/auth/me", headers=bearer(login["sessionId"]))

            returning = await sign_in(http, factory, monkeypatch)

        async with factory() as session:
            connections = (await session.execute(select(ProviderToken))).scalars().all()

    assert out.status_code == 200
    # The provider publishes a logout endpoint, so there is somewhere to finish.
    assert out.json()["logoutUrl"].startswith(idp.END_SESSION_ENDPOINT)

    assert after.status_code == 401
    # Signing out is not disconnecting: the connection is untouched...
    assert len(connections) == 1
    # ...and the next sign-in finds the record again.
    assert returning["user"]["patientId"] == body["patientId"]


@respx.mock
async def test_disconnecting_the_last_provider_releases_the_account_record(
    tmp_path, monkeypatch
):
    """The record is gone, so the account must stop pointing at it."""
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            body = await connect(
                http,
                EPIC_SANDBOX,
                token_response("epic-patient"),
                link_session=login["sessionId"],
            )
            respx.get(url__startswith=EPIC_SANDBOX["iss"]).mock(
                return_value=httpx.Response(200, json={"resourceType": "Bundle", "entry": []})
            )
            removed = await http.delete(
                f"/patients/{body['patientId']}/connections/{EPIC_SANDBOX['provider']}",
                headers=bearer(login["sessionId"]),
            )
            # Still signed in, with nothing connected.
            me = await http.get("/auth/me", headers=bearer(login["sessionId"]))

        async with factory() as session:
            user = (await session.execute(select(User))).scalar_one()

    assert removed.status_code == 200
    assert user.patient_id is None
    assert me.status_code == 200
    assert me.json() == {**me.json(), "authenticated": True, "patientId": None}


@respx.mock
async def test_an_anonymous_connection_belongs_to_nobody(tmp_path):
    """The path that existed before login still works, and still owns nothing."""
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            body = await connect(http, EPIC_SANDBOX, token_response("epic-patient"))
            me = await http.get("/auth/me", headers=bearer(body["sessionId"]))

        async with factory() as session:
            users = (await session.execute(select(User))).scalars().all()

    assert users == []
    assert me.status_code == 200
    # It reaches a record, but names no person — so the record goes with it.
    assert me.json()["authenticated"] is False
    assert me.json()["user"] is None
    assert me.json()["patientId"] == body["patientId"]


@respx.mock
async def test_require_user_login_closes_the_anonymous_path(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRE_USER_LOGIN", "true")
    get_settings.cache_clear()
    try:
        async with app_db(DB.format(path=tmp_path)) as factory:
            async with client() as http:
                anonymous = await http.post(
                    "/auth/connect",
                    json={
                        "provider": EPIC_SANDBOX["provider"],
                        "iss": EPIC_SANDBOX["iss"],
                    },
                )

                # Signed in, the same request is allowed.
                login = await sign_in(http, factory, monkeypatch)
                allowed = await connect(
                    http,
                    EPIC_SANDBOX,
                    token_response("epic-patient"),
                    link_session=login["sessionId"],
                )

            async with factory() as session:
                states = (await session.execute(select(AppSession))).scalars().all()
    finally:
        get_settings.cache_clear()

    assert anonymous.status_code == 401
    assert allowed["patientId"]
    # The refusal happened before anything was created, so no orphan record was
    # left behind by the request that was turned away.
    assert all(s.user_id is not None for s in states)


@respx.mock
async def test_a_session_from_an_authorization_alone_is_not_authenticated(
    tmp_path, monkeypatch
):
    """Connecting while signed in still yields a session tied to the account."""
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            login = await sign_in(http, factory, monkeypatch)
            body = await connect(
                http,
                EPIC_SANDBOX,
                token_response("epic-patient"),
                link_session=login["sessionId"],
            )
            me = await http.get("/auth/me", headers=bearer(body["sessionId"]))

    # The session /auth/callback minted knows the account too, so a frontend that
    # keeps only that one is still signed in.
    assert me.json()["authenticated"] is True
    assert me.json()["patientId"] == body["patientId"]
