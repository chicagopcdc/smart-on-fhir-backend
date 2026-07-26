"""Connecting providers to a patient record, and what a record grants access to.

A stored token is a connection: one person, at one provider, on one server. What
makes two connections the same person is the patient record they hang off, and
the only thing that can assert that is the person authorizing the second provider
while holding a session from the first. These tests drive that assertion through
the real endpoints, and then check the boundary it creates: a session reaches its
own record and nothing else.

Discovery and the token endpoint are served by respx from fixtures; everything
between them is the real application.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from app.auth.models import OAuthState, ProviderToken
from tests.app_harness import (
    CERNER_SANDBOX,
    SMART_LAUNCHER,
    app_db,
    client,
    connect,
    mock_server,
    token_response,
)


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'linking.db'}"


@respx.mock
async def test_a_first_connection_anchors_a_record_of_its_own(db_url):
    async with app_db(db_url):
        async with client() as http:
            body = await connect(http, SMART_LAUNCHER, token_response("launcher-p1"))

    assert body["patientId"].startswith("pat_")
    assert body["connection"] == {
        "provider": SMART_LAUNCHER["provider"],
        "iss": SMART_LAUNCHER["iss"],
        "patientFhirId": "launcher-p1",
    }
    # The record id is ours and the FHIR id is the server's; conflating them is
    # the mistake this whole model exists to prevent.
    assert body["patientId"] != body["connection"]["patientFhirId"]


@respx.mock
async def test_a_second_provider_joins_the_record_the_session_holds(db_url):
    async with app_db(db_url) as factory:
        async with client() as http:
            first = await connect(http, SMART_LAUNCHER, token_response("launcher-p1"))
            second = await connect(
                http,
                CERNER_SANDBOX,
                token_response("cerner-p9"),
                link_session=first["sessionId"],
            )

        async with factory() as session:
            tokens = (
                await session.execute(select(ProviderToken).order_by(ProviderToken.id))
            ).scalars().all()
            patient_ids = {token.patient_id for token in tokens}

    # Two connections, two servers, two unrelated FHIR ids, one record.
    assert first["patientId"] == second["patientId"]
    assert len(tokens) == 2
    assert patient_ids == {first["patientId"]}
    assert {token.patient_fhir_id for token in tokens} == {"launcher-p1", "cerner-p9"}


@respx.mock
async def test_connecting_without_a_session_starts_a_separate_record(db_url):
    async with app_db(db_url):
        async with client() as http:
            first = await connect(http, SMART_LAUNCHER, token_response("launcher-p1"))
            # Same browser, no session presented: nothing has claimed these are
            # the same person, so nothing may assume it.
            second = await connect(http, CERNER_SANDBOX, token_response("cerner-p9"))

    assert first["patientId"] != second["patientId"]


@respx.mock
async def test_authenticating_to_one_connection_does_not_hand_over_a_record(db_url):
    """The boundary a record has to hold.

    Someone authorizes a provider that another record already holds, presenting
    no session. They have proved control of that one connection and nothing else,
    so they must land on a record of their own — not on the one somebody else
    assembled around the same connection, which would give them every provider
    linked to it on the strength of a single login.

    This matters most where a provider authenticates weakly. On the public SMART
    Launcher any caller can pick any patient with no credentials at all, so a
    record that rejoined on sight would be readable by anyone who chose the same
    one.
    """
    async with app_db(db_url) as factory:
        async with client() as http:
            holder = await connect(http, SMART_LAUNCHER, token_response("shared-x"))
            await connect(
                http,
                CERNER_SANDBOX,
                token_response("cerner-private"),
                link_session=holder["sessionId"],
            )

            # A second caller, same EHR account, no session presented.
            other = await connect(http, SMART_LAUNCHER, token_response("shared-x"))

        async with factory() as session:
            rows = (await session.execute(select(ProviderToken))).scalars().all()

    assert other["patientId"] != holder["patientId"]

    # The Cerner connection the first caller linked stays on their record alone,
    # so the second caller's session reaches nothing they did not authorize.
    reachable = {r.provider for r in rows if r.patient_id == other["patientId"]}
    assert reachable == {SMART_LAUNCHER["provider"]}

    # The account is now held under both records, each with its own token. The
    # duplication is the isolation.
    launcher_rows = [r for r in rows if r.provider == SMART_LAUNCHER["provider"]]
    assert len(launcher_rows) == 2
    assert {r.patient_id for r in launcher_rows} == {
        holder["patientId"],
        other["patientId"],
    }


@respx.mock
async def test_reauthorizing_a_connection_the_caller_holds_updates_it_in_place(db_url):
    """Re-auth with the record's own session keeps the record and its providers."""
    async with app_db(db_url) as factory:
        async with client() as http:
            first = await connect(http, SMART_LAUNCHER, token_response("launcher-p1"))
            await connect(
                http,
                CERNER_SANDBOX,
                token_response("cerner-p9"),
                link_session=first["sessionId"],
            )
            again = await connect(
                http,
                CERNER_SANDBOX,
                token_response("cerner-p9"),
                link_session=first["sessionId"],
            )

        async with factory() as session:
            rows = (await session.execute(select(ProviderToken))).scalars().all()

    assert again["patientId"] == first["patientId"]
    # Updated in place rather than accumulating a second row for the same
    # connection on the same record.
    assert len(rows) == 2


@respx.mock
async def test_connect_refuses_an_invalid_session_rather_than_ignoring_it(db_url):
    """An expired bearer must not silently start a second record.

    Treating a bad session as "no session" would quietly split a patient's
    providers across two records, and the caller would have no way to tell.
    """
    async with app_db(db_url) as factory:
        async with client() as http:
            response = await http.post(
                "/auth/connect",
                json={
                    "provider": SMART_LAUNCHER["provider"],
                    "iss": SMART_LAUNCHER["iss"],
                },
                headers={"Authorization": "Bearer not-a-real-session"},
            )

        async with factory() as session:
            states = (await session.execute(select(OAuthState))).scalars().all()

    assert response.status_code == 401
    # Refused before anything was written, so a rejected link leaves no trace.
    assert states == []


@respx.mock
async def test_connect_refuses_an_issuer_the_provider_is_not_registered_for(db_url):
    async with app_db(db_url):
        async with client() as http:
            response = await http.post(
                "/auth/connect",
                json={
                    "provider": SMART_LAUNCHER["provider"],
                    "iss": "https://evil.example/fhir",
                },
            )

    assert response.status_code == 400
    assert response.json()["detail"] == "Issuer not allowed for this provider"
    # Refused before any request was made to the issuer it named.
    assert not respx.calls


@respx.mock
async def test_the_link_is_fixed_when_the_flow_starts_not_when_it_completes(db_url):
    """The record to join is settled at connect time, against a live session.

    The callback reads it from the state row rather than from the request, so a
    completed authorization cannot be steered into another record afterwards.
    """
    async with app_db(db_url) as factory:
        async with client() as http:
            first = await connect(http, SMART_LAUNCHER, token_response("launcher-p1"))

            mock_server(CERNER_SANDBOX, token_response("cerner-p9"))
            started = await http.post(
                "/auth/connect",
                json={
                    "provider": CERNER_SANDBOX["provider"],
                    "iss": CERNER_SANDBOX["iss"],
                },
                headers={"Authorization": f"Bearer {first['sessionId']}"},
            )
            state = started.json()["state"]

            async with factory() as session:
                row = await session.get(OAuthState, state)
                assert row.link_patient_id == first["patientId"]

            # Completing with a different session presented changes nothing: the
            # callback does not read the caller's bearer.
            other = await connect(http, SMART_LAUNCHER, token_response("launcher-p2"))
            completed = await http.post(
                "/auth/callback",
                json={"code": "test-auth-code", "state": state},
                headers={"Authorization": f"Bearer {other['sessionId']}"},
            )

    assert completed.status_code == 200
    assert completed.json()["patientId"] == first["patientId"]


@respx.mock
async def test_a_state_is_consumed_so_a_link_cannot_be_replayed(db_url):
    async with app_db(db_url):
        async with client() as http:
            first = await connect(http, SMART_LAUNCHER, token_response("launcher-p1"))

            mock_server(CERNER_SANDBOX, token_response("cerner-p9"))
            started = await http.post(
                "/auth/connect",
                json={
                    "provider": CERNER_SANDBOX["provider"],
                    "iss": CERNER_SANDBOX["iss"],
                },
                headers={"Authorization": f"Bearer {first['sessionId']}"},
            )
            state = started.json()["state"]

            first_use = await http.post(
                "/auth/callback", json={"code": "test-auth-code", "state": state}
            )
            replay = await http.post(
                "/auth/callback", json={"code": "test-auth-code", "state": state}
            )

    assert first_use.status_code == 200
    assert replay.status_code == 400
    assert replay.json()["detail"] == "Invalid or expired state"


@respx.mock
async def test_the_callback_still_answers_the_names_the_frontend_reads(db_url):
    """The pre-record keys stay until the frontend and demo have moved off them."""
    async with app_db(db_url):
        async with client() as http:
            body = await connect(http, SMART_LAUNCHER, token_response("launcher-p1"))

    assert body["success"] is True
    assert body["session_id"] == body["sessionId"]
    assert body["patient"] == body["connection"]["patientFhirId"]


@respx.mock
async def test_auth_start_still_redirects_the_browser(db_url):
    """The deprecated entry point keeps its redirect and its error shape."""
    async with app_db(db_url):
        async with client() as http:
            mock_server(SMART_LAUNCHER, token_response("launcher-p1"))
            redirected = await http.get(
                "/auth/start",
                params={
                    "provider": SMART_LAUNCHER["provider"],
                    "iss": SMART_LAUNCHER["iss"],
                },
                follow_redirects=False,
            )
            refused = await http.get(
                "/auth/start",
                params={"provider": "NOT_A_PROVIDER", "iss": SMART_LAUNCHER["iss"]},
            )

    assert redirected.status_code == 307
    assert redirected.headers["location"].startswith(SMART_LAUNCHER["authorize_url"])
    assert refused.status_code == 400
    assert refused.json() == {"error": "Unknown or unsupported provider"}


@respx.mock
async def test_a_provider_that_never_answers_leaves_no_state_behind(db_url):
    """A server we cannot read is a 502, not a half-started authorization."""
    respx.get(SMART_LAUNCHER["iss"] + "/.well-known/smart-configuration").mock(
        side_effect=httpx.ConnectError("refused")
    )

    async with app_db(db_url) as factory:
        async with client() as http:
            response = await http.post(
                "/auth/connect",
                json={
                    "provider": SMART_LAUNCHER["provider"],
                    "iss": SMART_LAUNCHER["iss"],
                },
            )

        async with factory() as session:
            states = (await session.execute(select(OAuthState))).scalars().all()

    assert response.status_code == 502
    assert states == []
