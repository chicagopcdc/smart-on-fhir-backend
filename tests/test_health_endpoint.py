"""`GET /health` exists for a supervisor, so every answer it can give matters.

A healthcheck that can only say yes is a healthcheck that never fires, so the
failure cases are the ones earning the endpoint its place: a store that refuses,
a store that accepts and then says nothing, and a second probe arriving while
the first is still stuck against it. What the endpoint is for is in
``app/api/health.py``.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy.exc import OperationalError

import app.api.health as _health_module
from app.core import db
from tests.app_harness import app_db as _app_db, client as _client

# Long enough that a handler which waits for the hang is unmistakable, short
# enough that a stray task cannot outlive the suite.
_HANG_SECONDS = 5


@pytest.fixture(autouse=True)
def _no_probe_in_flight(monkeypatch):
    """Start every test with the handler's guard clear.

    The guard is module state, so without this a probe one test deliberately
    strands would decide the next test's answer, and the order tests happen to
    run in would change the result.
    """
    monkeypatch.setattr(_health_module, "_in_flight", None)


def _cancel_stuck_probe() -> None:
    """Stop the probe a hang test deliberately stranded.

    Left alone it sits pending until the event loop closes under it, which
    asyncio reports as a task destroyed while pending.
    """
    probe = _health_module._in_flight
    if probe is not None and not probe.done():
        probe.cancel()


class _UnreachableStore:
    """A session that fails its first query the way a dropped Postgres does.

    Standing in for the database rather than taking one away: the endpoint's job
    is to turn that exception into an answer, and this is the exception.
    """

    async def execute(self, *_args, **_kwargs):
        raise OperationalError("SELECT 1", None, ConnectionRefusedError("refused"))


class _UnreachableFactory:
    """Stands in for ``SessionFactory``, which the handler opens itself.

    The endpoint opens its own session rather than taking one from a
    dependency, so there is nothing to override: substituting the factory is how
    a test reaches it.
    """

    async def __aenter__(self) -> _UnreachableStore:
        return _UnreachableStore()

    async def __aexit__(self, *_exc) -> bool:
        return False


class _HangingStore:
    """A session whose query never comes back."""

    async def execute(self, *_args, **_kwargs):
        await asyncio.sleep(_HANG_SECONDS)


class _HangingFactory:
    """Hangs on the way out as well as on the way in.

    The teardown is the half that matters: closing a session against a server
    that has stopped answering wants a round trip that never completes, so a
    handler which waits for the cleanup is stranded even though the query
    itself was bounded.
    """

    async def __aenter__(self) -> _HangingStore:
        return _HangingStore()

    async def __aexit__(self, *_exc) -> bool:
        await asyncio.sleep(_HANG_SECONDS)
        return False


async def test_health_reports_a_reachable_database(tmp_path):
    async with _app_db(f"sqlite+aiosqlite:///{tmp_path / 'health.db'}"):
        async with _client() as client:
            response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_health_answers_rather_than_raises_when_the_store_is_gone(monkeypatch):
    monkeypatch.setattr(db, "SessionFactory", _UnreachableFactory)

    async with _client() as client:
        response = await client.get("/health")

    # 503 is what a container runtime and a load balancer act on; the body is
    # for whoever goes looking afterwards. A 500 here would say the healthcheck
    # itself broke, which is a different and much less useful thing to report.
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "error"


async def test_health_answers_within_its_bound_when_the_store_never_replies(monkeypatch):
    # The failure the bound exists for, driven rather than mocked: a server
    # that accepts the connection and then goes quiet. Both the query and the
    # teardown hang here, because waiting on the teardown is what used to
    # strand this endpoint — against a frozen Postgres the first probe never
    # came back at all, while later ones answered in two seconds.
    monkeypatch.setattr(_health_module, "_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(db, "SessionFactory", _HangingFactory)

    started = time.perf_counter()
    async with _client() as client:
        response = await client.get("/health")
    elapsed = time.perf_counter() - started
    _cancel_stuck_probe()

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    # The assertion that matters. A handler that waits for the cleanup returns
    # in _HANG_SECONDS rather than in the bound it advertises.
    assert elapsed < _HANG_SECONDS / 2


async def test_health_does_not_open_a_second_connection_behind_a_stuck_probe(monkeypatch):
    # One wedged server should cost one connection, not one per probe. A
    # healthcheck runs on a timer, so without this the pool drains steadily
    # while the endpoint whose job is to report the problem causes a worse one.
    opened: list[None] = []

    class _CountingFactory(_HangingFactory):
        def __init__(self) -> None:
            opened.append(None)

    monkeypatch.setattr(_health_module, "_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(db, "SessionFactory", _CountingFactory)

    async with _client() as client:
        first = await client.get("/health")
        second = await client.get("/health")
    _cancel_stuck_probe()

    assert (first.status_code, second.status_code) == (503, 503)
    assert len(opened) == 1
