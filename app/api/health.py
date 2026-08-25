"""Whether the service is up, answered for a supervisor rather than a caller.

Nothing in the API needs this. A container runtime does: it has to tell a
process that is listening from a process that is working, and restart or hold
traffic off the second. The one thing worth checking is the store, because
``app/core/db.py`` opens no connection until a request asks it to — so an
otherwise healthy-looking process can be pointed at a Postgres that is not
there and only find out on someone's first authorization.

Unthrottled on purpose: a throttled probe answers 429, which a supervisor reads
as a failure and acts on, so the rate limiter could manufacture the restart it
was there to avoid. That leaves this the one unauthenticated route that always
reaches the database, which is why the query below is bounded rather than left
to run as long as it likes.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.api.schemas import HealthResponse
from app.core import db

router = APIRouter(tags=["health"])

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 2

# The probe currently in flight, if one has not come back yet. A wedged server
# is the case this exists for: the probe against it may never return, and
# starting a fresh one on every subsequent request would take another pooled
# connection to the same dead server until the pool was gone. One at a time
# means one connection is at risk, not all of them.
#
# Module state is safe here only because the guard below reads it and replaces
# it with no await in between, so no second request can interleave.
_in_flight: asyncio.Task | None = None


def _retrieve(task: asyncio.Task) -> None:
    """Read whatever a probe we stopped waiting for ended up raising.

    A task nobody asks about is reported by asyncio as an exception that was
    never retrieved, which would put a traceback in the log for a failure this
    endpoint has already handled and answered.
    """
    if not task.cancelled():
        task.exception()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Whether the service and its database are up",
    responses={
        503: {
            "model": HealthResponse,
            "description": "The process is answering but something it depends on is not.",
        }
    },
)
async def health(response: Response) -> HealthResponse:
    """Report the service's own state and whether Postgres answers.

    ``SELECT 1`` is the whole check: it costs a round trip and proves the pool
    can reach the server and authenticate, which is what every later query
    depends on. The status code, not the body, is what a probe reads, so it is
    set from the same verdict the body reports.
    """
    global _in_flight

    async def _probe() -> None:
        # The session is opened and closed inside the probe rather than taken
        # from a dependency, because FastAPI closes an injected one from its
        # AsyncExitStack after the handler has already returned, which puts the
        # teardown outside anything this handler can bound.
        #
        # Reached through the module rather than bound at import, because the
        # test harness redirects the database by rebinding db.SessionFactory.
        # A `from ... import SessionFactory` here would keep the process-wide
        # factory and quietly ignore that.
        async with db.SessionFactory() as session:
            await session.execute(text("SELECT 1"))

    if _in_flight is not None and not _in_flight.done():
        # An earlier probe has still not come back, which is itself the answer:
        # the store it is waiting on has not responded either. Reporting that
        # beats opening a second connection to the same wedged server.
        logger.warning("Health check skipped: an earlier probe has not returned")
        response.status_code = 503
        return HealthResponse(status="degraded", database="error")

    _in_flight = asyncio.create_task(_probe())
    _in_flight.add_done_callback(_retrieve)
    try:
        # asyncio.wait, not wait_for. wait_for cancels on timeout and then
        # *awaits* that cancellation, and the cleanup it waits for is exactly
        # what blocks against a server that accepts a connection and then stops
        # answering: closing the session wants a round trip to a socket that
        # will never reply. Measured against a frozen Postgres, the first probe
        # never returned at all. wait leaves the task alone, so the bound below
        # is the bound a caller actually sees.
        done, _pending = await asyncio.wait({_in_flight}, timeout=_TIMEOUT_SECONDS)
        if not done:
            # Cancelled but deliberately not awaited: awaiting is what put us
            # here. It unwinds in its own time, and the guard above keeps a
            # second probe from stacking up behind it.
            _in_flight.cancel()
            raise TimeoutError(f"no answer within {_TIMEOUT_SECONDS}s")
        exc = _in_flight.exception()
        if exc is not None:
            raise exc
    except Exception as exc:
        # Deliberately broad. Every narrower clause is a guess at which failures
        # a database can have, and the one it misses would leave the handler
        # whose entire job is to answer raising a 500 instead — which tells a
        # probe the check is broken rather than that the store is.
        #
        # What went wrong goes to the log and not to the caller. This endpoint
        # is unauthenticated and reachable by anything that can open the port,
        # so naming the failure would tell a stranger whether the database is
        # down, or up and refusing our password. The rest of this API already
        # draws that line: see _CHECK_DETAIL in providers.py, which fixes its
        # prose locally rather than repeating what an endpoint said, and the
        # 404-over-403 in patients.py. Whoever is on the other end of a red
        # healthcheck is reading the log anyway.
        # %r, not %s: several of the failures that land here carry an empty
        # str(), so the reason would otherwise log as a bare colon.
        logger.warning("Health check could not reach the database: %r", exc)
        response.status_code = 503
        return HealthResponse(status="degraded", database="error")

    return HealthResponse(status="ok", database="ok")
