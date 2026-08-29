"""A failure nothing modelled still answers something a browser can read.

Starlette builds its stack with ``ServerErrorMiddleware`` outside every
middleware the application adds, so an exception that unwinds that far produces
a 500 above ``CORSMiddleware`` and without its headers. The browser then reports
a CORS failure, and the status the server actually sent is not readable from
JavaScript at all — a 500 and a misconfigured origin look identical from the
frontend, which is the worst possible pair to confuse.

This was measured against the containerized stack with the database stopped:
``POST /auth/connect`` answered 500 with no ``access-control-allow-origin``,
while ``GET /health`` answered 503 *with* one, because health catches its own
failure and returns rather than raising.

So these drive the two shapes an uncaught failure takes — a store that has gone
away, and a stored token no configured key will decrypt — and check the answer is
readable, identifiable, and gives nothing away.
"""

from __future__ import annotations

import logging

import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import text, update

from app.auth.models import ProviderToken
from app.core.config import get_settings
from app.core.logging import build_handler, configure_logging
from tests.app_harness import (
    CERNER_SANDBOX as CERNER,
    SMART_LAUNCHER as LAUNCHER,
    app_db,
    bearer,
    client,
    connect_and_serve,
    load_fixture,
)

ORIGIN = "http://localhost:3000"


@pytest.fixture
def written():
    """The shipped handler over a buffer, so what is asserted is what is written."""
    import io

    buffer = io.StringIO()
    handler = build_handler()
    handler.setStream(buffer)
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    configure_logging()
    root.setLevel(logging.INFO)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


@respx.mock
async def test_a_failure_with_the_store_gone_still_answers_with_cors(written, tmp_path):
    """The measured case: the database is unreachable partway through a request."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'gone.db'}") as factory:
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER)

            # The store goes away under a live session, which is what stopping a
            # database container does to requests already in flight behind it.
            async with factory() as session:
                await session.execute(text("DROP TABLE provider_token"))
                await session.commit()

            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers={**bearer(connected["sessionId"]), "Origin": ORIGIN},
            )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert response.headers.get("access-control-allow-origin") == ORIGIN, (
        "the browser would see a CORS error instead of the server error"
    )
    assert response.headers.get("x-request-id"), "the failure cannot be looked up"

    logged = written.getvalue()
    assert response.headers["x-request-id"] in logged, (
        "the id the caller was given does not appear in the log they would be quoting it to"
    )
    assert "request.unhandled" in logged
    assert connected["sessionId"] not in logged


@respx.mock
async def test_the_failure_response_says_nothing_about_what_failed(written, tmp_path):
    """A 500 is a fact about the server, not a description of its internals."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'quiet.db'}") as factory:
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER)
            async with factory() as session:
                await session.execute(text("DROP TABLE provider_token"))
                await session.commit()

            response = await http.get(
                f"/patients/{connected['patientId']}/summary",
                headers=bearer(connected["sessionId"]),
            )

    body = response.text
    for leaked in ("provider_token", "sqlite", "Traceback", "OperationalError"):
        assert leaked not in body, f"the response names {leaked}"


async def test_an_allowed_origin_is_still_the_only_one_answered():
    """Catching failures must not have loosened what CORS lets through."""
    async with client() as http:
        allowed = await http.options(
            "/providers",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        forged = await http.options(
            "/providers",
            headers={
                "Origin": "https://not-the-frontend.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.headers.get("access-control-allow-origin") == ORIGIN
    assert "access-control-allow-origin" not in forged.headers


async def test_a_caller_supplied_request_id_is_used_only_when_it_is_safe():
    """An id from outside is worth adopting and cannot be trusted unchecked.

    It is written into log lines, so a value carrying a newline could fabricate a
    record and a long one could bury a real one.
    """
    async with client() as http:
        adopted = await http.get("/health", headers={"X-Request-ID": "edge-7f3a90"})
        forged = await http.get("/health", headers={"X-Request-ID": "a b\nINFO fake"})
        oversized = await http.get("/health", headers={"X-Request-ID": "x" * 200})

    assert adopted.headers["x-request-id"] == "edge-7f3a90"
    assert forged.headers["x-request-id"] != "a b\nINFO fake"
    assert "\n" not in forged.headers["x-request-id"]
    assert len(oversized.headers["x-request-id"]) <= 64


# --- a stored token no key will decrypt ---------------------------------------


async def _rekey(factory, provider: str) -> None:
    """Rewrite one connection's tokens under a key this process does not hold.

    What a rotation that drops a key still in use leaves behind, and reachable
    without any corruption at all: `.env` documents rotation as "prepend the new
    key, the rest are still accepted", so dropping the old one does exactly this.
    """
    stranded = Fernet(Fernet.generate_key())
    async with factory() as session:
        await session.execute(
            update(ProviderToken)
            .where(ProviderToken.provider == provider)
            # Written to the columns rather than through the properties, which
            # would helpfully re-encrypt under the key this process does hold and
            # leave nothing to fail on.
            .values(
                encrypted_access_token=stranded.encrypt(b"stranded-access").decode(),
                encrypted_refresh_token=stranded.encrypt(b"stranded-refresh").decode(),
            )
        )
        await session.commit()


@respx.mock
async def test_one_undecryptable_connection_leaves_the_rest_of_the_record_readable(
    written, tmp_path
):
    """The blast radius this closes: decryption used to fail while rows loaded.

    It happened inside the query, so the connection could not be listed, let alone
    reported as unreadable — and every healthy connection on the same record went
    down with it, as a 500 with no CORS headers.
    """
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'rotated.db'}") as factory:
        async with client() as http:
            first, _ = await connect_and_serve(http, LAUNCHER)
            await connect_and_serve(http, CERNER, link_session=first["sessionId"])
            await _rekey(factory, CERNER["provider"])

            response = await http.get(
                f"/patients/{first['patientId']}/resources",
                headers={**bearer(first["sessionId"]), "Origin": ORIGIN},
            )

    assert response.status_code == 200, response.text
    health = {c["provider"]: c for c in response.json()["connections"]}

    stranded = health[CERNER["provider"]]
    assert stranded["status"] == "error"
    # False on purpose. The tokens are fine and want the old key back; sending the
    # patient round the consent screen would overwrite them under the new key,
    # losing the evidence and clearing the symptom while the fault stands.
    assert stranded["needsReauthorization"] is False
    assert "attention" in stranded["error"]

    assert health[LAUNCHER["provider"]]["status"] == "ok", (
        "a key problem on one connection took a healthy one down with it"
    )
    assert health[LAUNCHER["provider"]]["resources"]["Condition"]["status"] == "ok"

    logged = written.getvalue()
    assert "connection.token.undecryptable" in logged, (
        "a key rotation that stranded a token left no trace"
    )
    assert CERNER["provider"] in logged
    assert "stranded-access" not in logged


@respx.mock
async def test_an_undecryptable_connection_can_still_be_disconnected(tmp_path):
    """The one a caller is most likely to be here to remove must be removable."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'removable.db'}") as factory:
        async with client() as http:
            first, _ = await connect_and_serve(http, LAUNCHER)
            await connect_and_serve(http, CERNER, link_session=first["sessionId"])
            await _rekey(factory, CERNER["provider"])

            ended = await http.delete(
                f"/patients/{first['patientId']}/connections/{CERNER['provider']}",
                headers=bearer(first["sessionId"]),
            )
            after = await http.get(
                f"/patients/{first['patientId']}/resources",
                headers=bearer(first["sessionId"]),
            )

    assert ended.status_code == 200, ended.text
    assert ended.json()["connectionsRemaining"] == 1
    # Never claimed: the token could not be read, so nothing could be revoked with it.
    assert ended.json()["revokedAtProvider"] is False
    assert [c["provider"] for c in after.json()["connections"]] == [LAUNCHER["provider"]]


@respx.mock
async def test_the_summary_survives_an_undecryptable_connection(tmp_path):
    """The other read path reaches the same rows and used to fail the same way."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'summary.db'}") as factory:
        async with client() as http:
            first, _ = await connect_and_serve(http, LAUNCHER)
            await connect_and_serve(http, CERNER, link_session=first["sessionId"])
            await _rekey(factory, CERNER["provider"])

            response = await http.get(
                f"/patients/{first['patientId']}/summary",
                headers=bearer(first["sessionId"]),
            )

    assert response.status_code == 200, response.text
    body = response.json()
    assert {c["provider"] for c in body["connections"]} == {
        LAUNCHER["provider"],
        CERNER["provider"],
    }
    # The healthy provider still fills the chart.
    assert any(
        item["provider"] == LAUNCHER["provider"]
        for section in body["sections"]
        for item in section["items"]
    )


def test_the_settings_this_runs_under_are_the_shipped_ones():
    """A guard on the fixtures above: they assert against the configured origin."""
    assert ORIGIN in get_settings().resolved_cors_origins
    assert load_fixture(LAUNCHER["smart_config"]), "the launcher capture is still readable"
