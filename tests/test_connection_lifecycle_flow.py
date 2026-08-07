"""What the sweep takes and, more importantly, what it leaves.

``provider_token`` is the one table with no expiry of its own. A connection is
meant to outlive the session that made it — a stored refresh token is the whole
point — so sweeping on "has no live session" would delete exactly the rows worth
keeping. What decides it instead is whether the connection could still be
brought back to life, and that grades how long it is given rather than whether it
is taken at all.

Both halves are driven here, because a sweep is only as good as the rows it
declines to touch. The clock is moved by hand: rows are aged the way weeks of
wall time would age them, and the retention window is narrowed where a test is
about the boundary itself.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select, update

from app import main
from app.auth.models import AppSession, ProviderToken, utcnow
from app.core.config import get_settings
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
    return f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}"


@pytest.fixture
def short_retention(monkeypatch):
    """Retain a connection for a day rather than a month.

    The window is a deployment's choice, so a test about the boundary sets its
    own and ages rows around it instead of asking what the default happens to be.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "connection_retention_days", 1)
    return settings


async def unread_since(factory, when, *, patient_id: str | None = None) -> None:
    """Age connections to a last read at ``when``, as wall clock would.

    The access token goes with it. Time does not move one and not the other, and
    a connection idle for weeks whose token expires in an hour is a state no
    clock could produce — the token would have to have been refreshed, which
    only a read does, and a read is what ``last_used_at`` records.

    ``patient_id`` ages one record while leaving the rest where they are, for
    the tests that turn on the sweep telling records apart.
    """
    statement = update(ProviderToken).values(
        last_used_at=when, expires_at=when + timedelta(hours=1)
    )
    if patient_id is not None:
        statement = statement.where(ProviderToken.patient_id == patient_id)
    async with factory() as session:
        await session.execute(statement)
        await session.commit()


async def expire_sessions(factory, *, patient_id: str | None = None) -> None:
    """Let issued sessions lapse, leaving the records they named unreachable."""
    statement = update(AppSession).values(expires_at=utcnow() - timedelta(seconds=1))
    if patient_id is not None:
        statement = statement.where(AppSession.patient_id == patient_id)
    async with factory() as session:
        await session.execute(statement)
        await session.commit()


async def count_connections(factory) -> int:
    async with factory() as session:
        return len((await session.execute(select(ProviderToken))).scalars().all())


async def sweep(http) -> None:
    """Run the sweep by doing the thing that runs it: authorize something.

    The sweep is folded into the authorization that creates these rows, so it
    happens exactly as often as the growth it answers and needs no scheduler.
    """
    await connect(http, CERNER, token_response("someone-unrelated"))


# --- what it takes ------------------------------------------------------------


@respx.mock
async def test_reconnecting_a_provider_does_not_grow_the_table_without_bound(db_url):
    """The case the sweep exists for.

    Authorizing without presenting a session anchors a *new* record every time,
    deliberately — rejoining the one the same EHR account already sits under
    would hand a caller every provider linked to it on the strength of one
    login. The cost is a record per attempt, and nothing but this reclaims them.
    """
    record = load_fixture(LAUNCHER["record"])

    async with app_db(db_url) as factory:
        async with client() as http:
            for _ in range(4):
                await connect(http, LAUNCHER, token_endpoint(record["patientId"]))
            assert await count_connections(factory) == 4

            # Weeks pass. Every session lapses, and with no way back into a
            # record whose sessions are gone, nothing can ever read these again.
            await expire_sessions(factory)
            await unread_since(factory, utcnow() - timedelta(days=60))

            await sweep(http)

        # Only the connection that ran the sweep, which is minutes old.
        [survivor] = await stored_connections(factory)

    assert survivor.provider == CERNER["provider"]


@respx.mock
async def test_a_connection_the_provider_refused_goes_without_waiting(db_url):
    """A dead grant is not made to sit out a month it can never come back from.

    The refusal already happened at the provider, which is a better answer than
    any window we could pick: the connection cannot serve another read however
    long it is kept.
    """
    async with app_db(db_url) as factory:
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER, on_refresh=refuse)

            # A read finds the token lapsed, is refused a refresh, and the row
            # is left holding nothing that can be spent again.
            await age_tokens(factory)
            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )
            assert response.json()["connections"][0]["needsReauthorization"] is True

            await expire_sessions(factory)
            await sweep(http)

        [survivor] = await stored_connections(factory)

    assert survivor.provider == CERNER["provider"], (
        "a connection the provider has finished with was kept anyway"
    )


def test_the_applications_own_log_records_are_not_dropped(monkeypatch):
    """The count above is only useful if it reaches a log in a real deployment.

    uvicorn configures three loggers of its own and leaves the root logger
    alone, so ``app.*`` inherits WARNING and every INFO record the application
    writes goes nowhere. ``caplog`` forces a level, so the test above passes
    either way and cannot notice — which is the whole reason this one exists.

    And only ours: httpx logs a line per request at INFO, and a read fans out
    over a dozen FHIR calls whose URLs carry the provider's own patient id, so
    turning the whole process up to INFO would log the one identifier the rest
    of this code keeps out of logs on purpose.
    """
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    monkeypatch.setattr(logging.getLogger("app"), "level", logging.NOTSET)
    assert not logging.getLogger("app.api.auth").isEnabledFor(logging.INFO)

    main.configure_logging()

    assert logging.getLogger("app.api.auth").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO), (
        "a read's FHIR request URLs, patient id and all, would be logged"
    )


@respx.mock
async def test_the_sweep_says_how_many_it_took(db_url, caplog):
    """How fast the table turns over should be visible rather than inferred."""
    record = load_fixture(LAUNCHER["record"])

    async with app_db(db_url) as factory:
        async with client() as http:
            for _ in range(3):
                await connect(http, LAUNCHER, token_endpoint(record["patientId"]))
            await expire_sessions(factory)
            await unread_since(factory, utcnow() - timedelta(days=60))

            with caplog.at_level(logging.INFO, logger="app.api.auth"):
                await sweep(http)

    assert "Retired 3 connection(s)" in caplog.text
    # Counts only: this is a log, and whose connections they were is not its business.
    assert record["patientId"] not in caplog.text


# --- what it leaves -----------------------------------------------------------


@respx.mock
async def test_reconnecting_an_abandoned_provider_does_not_sweep_what_it_just_stored(
    db_url,
):
    """Reconnecting is how a caller rescues a record, not how they lose it.

    Coming back to a provider left alone for months is the ordinary reason to
    authorize one again, and by then the record is exactly what the sweep is
    looking for: nothing has read it, and its token expired long ago. The
    authorization repairs all of that — and then runs the sweep in the same
    request, which without care sees a record still wearing every mark of
    abandonment, because what redeems it has not been written yet.

    The session presented at the start is what says which record to rejoin, and
    it can lapse while the patient is still on the EHR's login page, so it
    cannot be relied on to be holding the record by the time the callback lands.
    """
    record = load_fixture(LAUNCHER["record"])
    # A server that states no lifetime for its access token. Legal, and it means
    # the row has no future expiry to vouch for itself with.
    undated = token_response(record["patientId"], expires_in=None)

    async with app_db(db_url) as factory:
        async with client() as http:
            first = await connect(http, LAUNCHER, undated)
            respx.get(url__startswith=LAUNCHER["iss"]).mock(
                side_effect=serve_record(record)
            )

            # Months pass: unread, long expired, no session left holding it.
            await unread_since(factory, utcnow() - timedelta(days=45))
            async with factory() as session:
                await session.execute(
                    update(ProviderToken).values(
                        created_at=utcnow() - timedelta(days=45), expires_at=None
                    )
                )
                await session.commit()

            # The patient comes back and reconnects, presenting the session they
            # still hold, so this rejoins the record rather than starting a new one.
            started = await http.post(
                "/auth/connect",
                json={"provider": LAUNCHER["provider"], "iss": LAUNCHER["iss"]},
                headers=bearer(first["sessionId"]),
            )
            assert started.status_code == 200, started.text

            # They take their time over the EHR login, and the session that named
            # the record lapses while they are on it.
            await expire_sessions(factory)

            callback = await http.post(
                "/auth/callback",
                json={"code": "test-auth-code", "state": started.json()["state"]},
            )
            assert callback.status_code == 200, callback.text
            reconnected = callback.json()

            reread = await http.get(
                f"/patients/{reconnected['patientId']}/resources",
                headers=bearer(reconnected["sessionId"]),
            )

        remaining = await stored_connections(factory)

    assert reconnected["patientId"] == first["patientId"]
    assert remaining, "the authorization swept the connection it had just stored"
    assert reread.status_code == 200, (
        "the callback handed back a session for a record it had already deleted"
    )
    assert reread.json()["connections"][0]["status"] == "ok"


@respx.mock
async def test_a_live_session_keeps_a_record_however_idle_it_looks(db_url):
    """Reachability wins outright over any window a deployment configures.

    A connection authorized a minute ago has never been read, which is what an
    abandoned one looks like too. The session is what tells them apart.
    """
    record = load_fixture(LAUNCHER["record"])

    async with app_db(db_url) as factory:
        async with client() as http:
            await connect(http, LAUNCHER, token_endpoint(record["patientId"]))
            # Old beyond any retention window, but still held by a live session.
            await unread_since(factory, utcnow() - timedelta(days=3650))

            await sweep(http)

        assert await count_connections(factory) == 2


@respx.mock
async def test_one_records_live_session_does_not_shelter_another(db_url):
    """The session has to belong to the record it is protecting.

    Every other test here either expires every session or keeps the one on the
    record it is watching, so a check that asked "is *any* session live" would
    pass all of them — and would then spare every abandoned record in the
    database for as long as one unrelated person was logged in, or empty the
    table the moment nobody was. This is the case that tells the two apart:
    one record held, one abandoned, both swept over at once.
    """
    async with app_db(db_url) as factory:
        async with client() as http:
            held, _ = await connect_and_serve(http, LAUNCHER)
            abandoned, _ = await connect_and_serve(http, LAUNCHER)

            # Both records are ancient. Only one still has anyone holding it.
            await unread_since(factory, utcnow() - timedelta(days=60))
            await expire_sessions(factory, patient_id=abandoned["patientId"])

            await sweep(http)

            still_readable = await http.get(
                f"/patients/{held['patientId']}/resources",
                headers=bearer(held["sessionId"]),
            )

        remaining = {c.patient_id for c in await stored_connections(factory)}

    assert abandoned["patientId"] != held["patientId"]
    assert abandoned["patientId"] not in remaining, "an unreachable record was sheltered"
    assert held["patientId"] in remaining, "a record someone still holds was swept"
    assert still_readable.status_code == 200


@respx.mock
async def test_reading_a_record_is_what_keeps_it(db_url, short_retention):
    """A connection someone is using never ages, whatever the window is set to.

    This is why the sweep can be aggressive without being dangerous: every read
    resets the clock across the whole record, so the window only ever runs out
    on a connection nobody has touched.
    """
    async with app_db(db_url) as factory:
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER)
            await unread_since(factory, utcnow() - timedelta(days=30))

            read = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )
            assert read.status_code == 200

            await expire_sessions(factory)
            await sweep(http)

        surviving = {connection.provider for connection in await stored_connections(factory)}

    assert LAUNCHER["provider"] in surviving, "a connection that was just read was swept"


@respx.mock
async def test_a_usable_refresh_token_buys_the_full_window(db_url, short_retention):
    """The boundary, from both sides, with only the idle time differing.

    A connection that can still be refreshed is what a downstream consumer holds
    between reads, so it survives a gap the same connection would not survive if
    its grant were gone.
    """
    record = load_fixture(LAUNCHER["record"])

    async with app_db(db_url) as factory:
        async with client() as http:
            await connect(http, LAUNCHER, token_endpoint(record["patientId"]))
            await expire_sessions(factory)

            # Inside the window: kept, unreachable session or not.
            await unread_since(factory, utcnow() - timedelta(hours=12))
            await sweep(http)
            assert await count_connections(factory) == 2

            # Past it: taken.
            await expire_sessions(factory)
            await unread_since(factory, utcnow() - timedelta(days=2))
            await sweep(http)

        surviving = {connection.provider for connection in await stored_connections(factory)}

    assert LAUNCHER["provider"] not in surviving


@respx.mock
async def test_a_record_whose_last_connection_goes_goes_with_it(db_url):
    """A record has no row of its own, so it ends when nothing hangs off it.

    Nothing has to delete it, and nothing could: an id that names no connections
    and no sessions is not a record, it is a string.
    """
    record = load_fixture(LAUNCHER["record"])

    async with app_db(db_url) as factory:
        async with client() as http:
            connected = await connect(http, LAUNCHER, token_endpoint(record["patientId"]))
            await expire_sessions(factory)
            await unread_since(factory, utcnow() - timedelta(days=60))
            await sweep(http)

        async with factory() as session:
            connections = await session.execute(
                select(ProviderToken).where(
                    ProviderToken.patient_id == connected["patientId"]
                )
            )
            sessions = await session.execute(
                select(AppSession).where(
                    AppSession.patient_id == connected["patientId"]
                )
            )

    assert connections.first() is None
    assert sessions.first() is None, "a bearer was left naming a record that is gone"


@respx.mock
async def test_one_record_being_swept_does_not_disturb_another(db_url):
    """Two people, one abandoned record and one live: only the abandoned one goes."""
    record = load_fixture(LAUNCHER["record"])

    async with app_db(db_url) as factory:
        async with client() as http:
            abandoned = await connect(http, LAUNCHER, token_endpoint(record["patientId"]))
            await expire_sessions(factory)
            await unread_since(factory, utcnow() - timedelta(days=60))

            # Someone else authorizes the same account, which anchors a record of
            # their own, and that is also what runs the sweep.
            kept, _ = await connect_and_serve(http, LAUNCHER)

            response = await http.get(
                f"/patients/{kept['patientId']}/resources",
                headers=bearer(kept["sessionId"]),
            )

        remaining = {connection.patient_id for connection in await stored_connections(factory)}

    assert response.status_code == 200
    assert response.json()["connections"][0]["status"] == "ok"
    assert remaining == {kept["patientId"]}
    assert abandoned["patientId"] != kept["patientId"]


@pytest.mark.parametrize("keeps_a_refresh_token", [False, True])
@respx.mock
async def test_a_connection_authorized_moments_ago_is_never_swept(
    db_url, monkeypatch, keeps_a_refresh_token
):
    """The sweep runs inside the very request that stores a connection.

    Between the token being committed and the session that reads it being
    issued, the row is reachable by nothing at all, and a rule keyed only on
    reachability would take it there — deleting a connection in the act of
    creating it. Its own unexpired access token is what carries it through.

    Both shapes, because they take different routes to the same guard and only
    one of them was ever protected. A retention window of zero is what makes the
    difference visible: the idle test it turns into (``created_at < now``) is
    true microseconds after the insert, so a connection with a refresh token
    would be swept on the strength of being new. Zero is a legitimate setting —
    keep nothing nobody is using — and it must not mean keep nothing at all.
    """
    monkeypatch.setattr(get_settings(), "connection_retention_days", 0)
    record = load_fixture(LAUNCHER["record"])
    granted = (
        token_endpoint(record["patientId"])
        if keeps_a_refresh_token
        # No refresh token at all: nothing to renew from, so this connection is
        # worth its access token and not a second longer.
        else token_response(record["patientId"])
    )

    async with app_db(db_url) as factory:
        async with client() as http:
            connected = await connect(http, LAUNCHER, granted)

        async with factory() as session:
            [connection] = (
                (await session.execute(select(ProviderToken))).scalars().all()
            )

    assert (connection.refresh_token is not None) is keeps_a_refresh_token
    assert connection.patient_id == connected["patientId"]
    assert connection.last_used_at is None, "nothing has read it, and it is still here"


@respx.mock
async def test_a_connection_that_cannot_be_renewed_lives_as_long_as_its_token(db_url):
    """The shortest case, from both sides of the only boundary it has.

    A connection with no refresh token is worth exactly its access token: while
    that works it is a live connection whether or not a session survives, and
    once it does not there is nothing left to keep.
    """
    record = load_fixture(LAUNCHER["record"])

    async with app_db(db_url) as factory:
        async with client() as http:
            await connect(http, LAUNCHER, token_response(record["patientId"]))
            # Sessions gone, but the access token has most of its hour left.
            await expire_sessions(factory)
            await sweep(http)
            assert await count_connections(factory) == 2

            await expire_sessions(factory)
            await age_tokens(factory)
            await sweep(http)

        surviving = {connection.provider for connection in await stored_connections(factory)}

    assert LAUNCHER["provider"] not in surviving


@respx.mock
async def test_the_sweep_does_not_call_out_to_a_provider(db_url):
    """Testing a refresh token by spending one would be the strongest signal.

    It would also put a provider's outage on the critical path of every
    authorization, so the sweep decides from what is already on the row. respx
    refusing an unmocked call is what proves it.
    """
    record = load_fixture(LAUNCHER["record"])

    async with app_db(db_url) as factory:
        async with client() as http:
            await connect(http, LAUNCHER, token_endpoint(record["patientId"]))
            await expire_sessions(factory)
            await unread_since(factory, utcnow() - timedelta(days=60))

            await sweep(http)

        surviving = {connection.provider for connection in await stored_connections(factory)}

    assert LAUNCHER["provider"] not in surviving
    assert refreshes() == [], "the sweep spent a refresh token to make its decision"


@respx.mock
async def test_a_token_endpoint_being_down_is_not_a_reason_to_sweep(db_url):
    """A provider having a bad day must not cost anyone their connection.

    The read reports it and moves on; the row keeps the refresh token it was
    refused a chance to spend, so the sweep still sees a revivable connection.
    """

    def outage(_granted) -> httpx.Response:
        return httpx.Response(503, text="down for maintenance")

    async with app_db(db_url) as factory:
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER, on_refresh=outage)
            await age_tokens(factory)

            read = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )
            assert read.json()["connections"][0]["needsReauthorization"] is False

            await expire_sessions(factory)
            await sweep(http)

        surviving = {connection.provider for connection in await stored_connections(factory)}

    assert LAUNCHER["provider"] in surviving
