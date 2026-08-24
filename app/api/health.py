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
from app.core.db import SessionFactory

router = APIRouter(tags=["health"])

logger = logging.getLogger(__name__)


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
    async def _probe() -> None:
        # Opened and closed entirely within this coroutine so that the
        # wait_for below bounds both the query and the session teardown.
        # FastAPI's dependency-injected sessions are closed by its
        # AsyncExitStack *after* the handler returns to the ASGI layer,
        # which means an unreachable Postgres can block the 503 from
        # reaching the probe long after the query itself timed out.
        async with SessionFactory() as session:
            await session.execute(text("SELECT 1"))

    try:
        # Bounded here rather than left to whoever is probing. A compose timeout
        # kills the prober, not the query, so against a Postgres that is
        # unreachable rather than refusing — packets dropped, or a wedged server
        # — this would hold a pooled connection until that query gave up, while
        # the next probe arrives and takes another. The pool is finite, so the
        # endpoint whose job is to answer would be the one to exhaust it.
        await asyncio.wait_for(_probe(), timeout=2)
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
        # %r, not %s: asyncio.wait_for raises a TimeoutError whose str() is empty,
        # so the one failure the bound above exists for would log a bare colon.
        logger.warning("Health check could not reach the database: %r", exc)
        response.status_code = 503
        return HealthResponse(status="degraded", database="error")

    return HealthResponse(status="ok", database="ok")
