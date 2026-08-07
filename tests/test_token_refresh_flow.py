"""A connection outliving its access token, driven through the real endpoints.

An access token lasts about an hour; the refresh token stored beside it lasts
days. The only thing between a connection authorized on Monday and a read on
Thursday is whether the read notices the first has run out and spends the second.

The clock is never waited on — the stored expiry is aged by hand, which is what
an hour of wall time would have done to it. Everything else is the real
application: the real routes, the real adapter, and a token endpoint that
answers a refresh the way a server issuing rotating refresh tokens does.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import update

from app.auth.models import ProviderToken, utcnow
from tests.app_harness import (
    CERNER_SANDBOX as CERNER,
    SMART_LAUNCHER as LAUNCHER,
    age_tokens,
    app_db,
    bearer,
    client,
    connect,
    connect_and_serve,
    load_fixture,
    refreshes,
    refuse,
    serve_record,
    stored_connections,
    token_endpoint,
    token_response,
)


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'refresh.db'}"


# --- renewing a token a read would otherwise send dead ------------------------


@respx.mock
async def test_a_read_renews_an_access_token_that_has_run_out(db_url):
    async with app_db(db_url) as factory:
        async with client() as http:
            connected, fhir = await connect_and_serve(http, LAUNCHER)
            await age_tokens(factory)

            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )

    assert response.status_code == 200, response.text
    [connection] = response.json()["connections"]
    # A real read, not a graceful empty one: the renewed token bought data.
    assert connection["status"] == "ok"
    assert connection["needsReauthorization"] is False
    assert connection["resources"]["Condition"]["entries"]

    assert [body["refresh_token"] for body in refreshes()] == ["refresh-1"]
    # And every FHIR call carried the token the refresh returned, not the one
    # the authorization did.
    assert fhir.called
    assert {call.request.headers["Authorization"] for call in fhir.calls} == {
        "Bearer access-2"
    }


@respx.mock
async def test_a_token_inside_the_leeway_is_renewed_before_it_lapses(db_url):
    """The window is what stops a read racing the expiry it just checked.

    A token with half a minute left passes an ``expires_at > now`` test and then
    dies partway through the dozen FHIR calls that follow.
    """
    async with app_db(db_url) as factory:
        async with client() as http:
            connected, fhir = await connect_and_serve(http, LAUNCHER)
            await age_tokens(factory, to=utcnow() + timedelta(seconds=30))

            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )

    assert response.json()["connections"][0]["status"] == "ok"
    assert len(refreshes()) == 1
    assert {call.request.headers["Authorization"] for call in fhir.calls} == {
        "Bearer access-2"
    }


@respx.mock
async def test_a_rotated_refresh_token_is_what_the_next_refresh_spends(db_url):
    """Storing the replacement is the difference between one refresh and every one.

    A server that rotates has retired the token it was handed. Keeping the old
    one leaves this read working and every read after it failing, which is the
    kind of failure that looks like it worked.
    """
    async with app_db(db_url) as factory:
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER)

            for _ in range(2):
                await age_tokens(factory)
                response = await http.get(
                    f"/patients/{connected['patientId']}/resources",
                    headers=bearer(connected["sessionId"]),
                )
                assert response.json()["connections"][0]["status"] == "ok"

        [connection] = await stored_connections(factory)

    assert [body["refresh_token"] for body in refreshes()] == [
        "refresh-1",
        "refresh-2",
    ], "the second refresh spent a token the server had already retired"
    assert connection.refresh_token == "refresh-3"


@respx.mock
async def test_two_reads_racing_one_expired_connection_refresh_once(db_url):
    """A refresh token is single-use on a rotating server, so it is spent once.

    Two reads arriving together on a lapsed token must not each present it. The
    second would be refused, and a server that treats a replayed refresh token
    as theft revokes the whole grant over it.
    """
    record = load_fixture(LAUNCHER["record"])
    answer = token_endpoint(record["patientId"])

    async def slowly(request: httpx.Request) -> httpx.Response:
        # Long enough that the second read certainly arrives while the first is
        # still on the wire, which is the race worth pinning.
        if b"grant_type=refresh_token" in request.content:
            await asyncio.sleep(0.05)
        return answer(request)

    async with app_db(db_url) as factory:
        async with client() as http:
            connected = await connect(http, LAUNCHER, slowly)
            respx.get(url__startswith=LAUNCHER["iss"]).mock(
                side_effect=serve_record(record)
            )
            await age_tokens(factory)

            both = await asyncio.gather(
                *(
                    http.get(
                        f"/patients/{connected['patientId']}/resources",
                        headers=bearer(connected["sessionId"]),
                    )
                    for _ in range(2)
                )
            )

    assert [response.json()["connections"][0]["status"] for response in both] == [
        "ok",
        "ok",
    ], "one of the two racing reads was left without a usable token"
    assert len(refreshes()) == 1


# --- when the provider will not renew it --------------------------------------


@respx.mock
async def test_a_refused_refresh_asks_for_re_authorization(db_url):
    """A revoked grant is a different answer from a provider having a bad day.

    The caller can do something about this one and only this one, so it is
    reported as itself rather than as the generic read failure the connection
    would otherwise look like.
    """
    async with app_db(db_url) as factory:
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER, on_refresh=refuse)
            await age_tokens(factory)

            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )

        [connection] = await stored_connections(factory)

    assert response.status_code == 200
    [reported] = response.json()["connections"]
    assert reported["status"] == "error"
    assert reported["needsReauthorization"] is True
    assert "no longer authorized" in reported["error"]

    # The refused token is not kept. It can never work again, and the row is now
    # visibly one that only a fresh authorization repairs.
    assert connection.refresh_token is None


@respx.mock
async def test_a_refused_refresh_does_not_sink_the_record(db_url):
    """One dead grant is not a patient's whole record.

    Reporting per connection is the point: the provider that still answers has
    to keep filling the chart while the other asks to be reconnected.
    """
    async with app_db(db_url) as factory:
        async with client() as http:
            first, _ = await connect_and_serve(http, LAUNCHER, on_refresh=refuse)
            await connect_and_serve(http, CERNER, link_session=first["sessionId"])
            await age_tokens(factory)

            response = await http.get(
                f"/patients/{first['patientId']}/summary",
                headers=bearer(first["sessionId"]),
            )

    body = response.json()
    health = {entry["provider"]: entry for entry in body["connections"]}
    assert health[LAUNCHER["provider"]]["needsReauthorization"] is True

    # Cerner renewed on its own refresh token, unaffected by the other's refusal,
    # and its half of the chart still arrived.
    assert [entry["refresh_token"] for entry in refreshes(CERNER)] == ["refresh-1"]
    assert health[CERNER["provider"]]["needsReauthorization"] is False
    assert any(
        item["provider"] == CERNER["provider"]
        for section in body["sections"]
        for item in section["items"]
    )


@respx.mock
async def test_a_refusal_of_a_token_already_replaced_is_not_the_patients_problem(db_url):
    """A refusal speaks for the token that was presented, and for nothing else.

    Refreshes are coalesced within a process, but two workers can reach the
    token endpoint with the same refresh token before either has stored a
    result. The one that arrives second is refused — correctly, the first spent
    it — and the row it would write to already holds a token it never saw.

    Not overwriting the winner's token is half the answer. The other half is not
    telling a patient to reconnect a provider whose connection was, moments ago,
    successfully renewed.
    """
    record = load_fixture(LAUNCHER["record"])
    exchange = token_endpoint(record["patientId"])

    async def refused_because_another_refresh_won(request: httpx.Request) -> httpx.Response:
        if b"grant_type=refresh_token" not in request.content:
            return exchange(request)
        # Stand in for the worker that got there first: by the time this refusal
        # lands, the row holds a token this request never presented.
        async with factory() as session:
            await session.execute(
                update(ProviderToken).values(
                    access_token="access-from-the-winner",
                    refresh_token="refresh-from-the-winner",
                    expires_at=utcnow() + timedelta(hours=1),
                )
            )
            await session.commit()
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with app_db(db_url) as factory:
        async with client() as http:
            connected = await connect(http, LAUNCHER, refused_because_another_refresh_won)
            fhir = respx.get(url__startswith=LAUNCHER["iss"]).mock(
                side_effect=serve_record(record)
            )
            await age_tokens(factory)

            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )

        [connection] = await stored_connections(factory)

    [reported] = response.json()["connections"]
    assert reported["needsReauthorization"] is False, (
        "a refusal of a token already retired asked the patient to reconnect"
    )
    assert reported["status"] == "ok"
    # The read went out on the winner's token, and the winner's token survived.
    assert {call.request.headers["Authorization"] for call in fhir.calls} == {
        "Bearer access-from-the-winner"
    }
    assert connection.refresh_token == "refresh-from-the-winner"


@respx.mock
async def test_a_granted_refresh_still_lands_when_a_refused_sibling_got_there_first(
    db_url,
):
    """The same race in the other order, which is the one that could brick a row.

    Two workers present the same refresh token; the provider rotates, so it
    honours one and refuses the other. Nothing orders the two *replies* — and a
    refusal is cheaper to answer than a rotation, so the refused worker can
    easily reach the row first and clear the token it was refused over.

    What arrives second is a real token set the provider has already committed
    to. Reading the cleared column as "someone else won" would discard it, and
    the connection would be left holding neither: an expired access token, no
    refresh token, and a report that the patient must reconnect a provider whose
    grant was alive the whole time. An empty column is the absence of a
    replacement, not evidence of one.
    """
    record = load_fixture(LAUNCHER["record"])
    exchange = token_endpoint(record["patientId"])
    refused_sibling_has_given_up = False

    async def granted_after_a_sibling_was_refused(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal refused_sibling_has_given_up
        if b"grant_type=refresh_token" not in request.content:
            return exchange(request)
        if not refused_sibling_has_given_up:
            refused_sibling_has_given_up = True
            # Stand in for the worker the provider refused, which reached the
            # row first and dropped the token it was refused over.
            async with factory() as session:
                await session.execute(update(ProviderToken).values(refresh_token=None))
                await session.commit()
        return exchange(request)

    async with app_db(db_url) as factory:
        async with client() as http:
            connected = await connect(http, LAUNCHER, granted_after_a_sibling_was_refused)
            respx.get(url__startswith=LAUNCHER["iss"]).mock(
                side_effect=serve_record(record)
            )

            await age_tokens(factory)
            first = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )
            # The connection has to still be refreshable afterwards, which is
            # the whole of what was at stake.
            await age_tokens(factory)
            second = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )

        [connection] = await stored_connections(factory)

    assert first.json()["connections"][0]["needsReauthorization"] is False
    assert second.json()["connections"][0]["needsReauthorization"] is False
    assert connection.refresh_token is not None, "the granted token set was thrown away"
    assert [body["refresh_token"] for body in refreshes()] == [
        "refresh-1",
        "refresh-2",
    ], "the second refresh had nothing to spend"


@respx.mock
async def test_a_token_endpoint_outage_keeps_a_working_refresh_token(db_url):
    """A 503 says nothing about the grant, so nothing is thrown away over one.

    Treating every refusal alike would cost a patient their connection every
    time a provider had a bad minute, or a client secret was rotated wrong.
    """

    def outage(_granted) -> httpx.Response:
        return httpx.Response(503, text="down for maintenance")

    async with app_db(db_url) as factory:
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER, on_refresh=outage)
            await age_tokens(factory)

            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )

        [connection] = await stored_connections(factory)

    [reported] = response.json()["connections"]
    assert reported["status"] == "error"
    # Not something the caller can fix by authorizing again, so it is not asked of them.
    assert reported["needsReauthorization"] is False
    assert "try again shortly" in reported["error"]
    assert connection.refresh_token == "refresh-1", "an outage cost a live grant"


@respx.mock
async def test_a_refresh_answering_with_a_different_patient_is_refused(db_url):
    """A connection is one person at one server, and a refresh cannot move it.

    No server does this. That is exactly why one that did would go unnoticed:
    the record would quietly start serving a chart nobody authorized.
    """

    def someone_else(granted) -> httpx.Response:
        return httpx.Response(200, json=granted(patient="a-different-patient"))

    async with app_db(db_url) as factory:
        async with client() as http:
            connected, _ = await connect_and_serve(
                http, LAUNCHER, on_refresh=someone_else
            )
            await age_tokens(factory)

            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )

        [connection] = await stored_connections(factory)

    [reported] = response.json()["connections"]
    assert reported["needsReauthorization"] is True
    assert connection.patient_fhir_id == connected["connection"]["patientFhirId"]
    assert connection.access_token == "access-1", "the mismatched token set was stored"


@respx.mock
async def test_a_connection_with_no_refresh_token_degrades_as_before(db_url):
    """A server that never granted offline_access has nothing to renew from.

    There is no refresh to attempt and no provider to blame, so the answer is
    the one it always was: the token has run out, connect the provider again.
    """
    record = load_fixture(LAUNCHER["record"])

    async with app_db(db_url) as factory:
        async with client() as http:
            connected = await connect(http, LAUNCHER, token_response(record["patientId"]))
            respx.get(url__startswith=LAUNCHER["iss"]).mock(
                return_value=httpx.Response(401, text="token expired")
            )
            await age_tokens(factory)

            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )

    assert refreshes() == [], "a connection with nothing to spend spent something"
    [reported] = response.json()["connections"]
    assert reported["status"] == "error"
    assert reported["needsReauthorization"] is True
    assert reported["error"] == "The stored token has expired; reconnect this provider"
