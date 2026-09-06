"""One middleware around every request: an id to gather its lines by, and a floor.

Both jobs are the same job. A request that fails in a way no route modelled is
exactly the one nobody can diagnose afterwards, so it needs a log line naming
what happened, an id tying that line to the response the caller saw, and a
response the caller can actually read.

That last part is why this is a middleware rather than an exception handler.
Starlette builds its stack as ``ServerErrorMiddleware`` outermost, then whatever
the application added, then ``ExceptionMiddleware``. An uncaught exception
unwinds past every middleware to the outermost one, so the 500 it produces is
built *above* ``CORSMiddleware`` and can never carry an
``Access-Control-Allow-Origin`` header — the browser reports a CORS failure and
the status the server actually sent is not readable from JavaScript at all. This
was measured against the containerized stack with the database stopped:
``POST /auth/connect`` answered 500 with no such header while ``GET /health``
answered 503 *with* one, because health catches its own failure and returns
rather than raising.

Catching here fixes it for every route at once, because a response built inside
the stack is still on its way out through ``CORSMiddleware`` and picks up the
configured origin like any other. The alternative doing the rounds — an
``@app.exception_handler(Exception)`` that writes
``access-control-allow-origin: *`` by hand — answers a different question, since
it hands the header to every origin rather than the ones the deployment allows.
"""

import logging
import re
import uuid

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.schemas import ErrorResponse
from app.core.logging import fields, request_id

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"

# What an inbound request id may look like. A proxy that assigns one is worth
# adopting, since it is what correlates our lines with everyone else's — but the
# value is written into log lines, so an unvalidated one is a way to forge them:
# newlines to fabricate a record, hundreds of characters to bury a real one.
_ACCEPTABLE_ID = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


def _request_id(scope: Scope) -> str:
    """The id to serve this request under: the caller's if it is usable, else ours."""
    candidate = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
    return candidate if _ACCEPTABLE_ID.match(candidate) else uuid.uuid4().hex


class RequestContext:
    """Bind a request id for the duration of one request, and catch what escapes.

    Written against the ASGI interface rather than as a ``BaseHTTPMiddleware``
    subclass, which runs the downstream application in a separate task: that
    breaks the ContextVar this sets, since a value bound in one task is not
    visible in another.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        current = _request_id(scope)
        token = request_id.set(current)
        header = (REQUEST_ID_HEADER.encode("latin-1"), current.encode("latin-1"))
        responded = False

        async def send_with_id(message: Message) -> None:
            nonlocal responded
            if message["type"] == "http.response.start":
                responded = True
                message["headers"] = [*message.get("headers", []), header]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            # The type and the frames, never str(exc): the message of whatever got
            # this far quotes the value that caused it, and on this application
            # that is a patient's data as often as it is a URL.
            logger.exception(
                "Unhandled %s serving a request",
                type(exc).__name__,
                **fields(
                    event="request.unhandled",
                    method=scope.get("method"),
                    # The path and not the raw query string, which on this API
                    # carries an issuer a caller chose and, on a FHIR search, a
                    # provider's patient id.
                    path=scope.get("path"),
                ),
            )
            if responded:
                # The status line is already on the wire, so there is no response
                # left to replace it with. Let it unwind and be logged as a broken
                # stream rather than pretending it was answered.
                raise
            # Built from the model the API declares its refusals with, so this
            # response — the one no route lists in its ``responses`` — cannot be
            # the one that drifts from the shape every other refusal keeps.
            await JSONResponse(
                ErrorResponse(detail="Internal server error").model_dump(),
                status_code=500,
                headers={REQUEST_ID_HEADER: current},
            )(scope, receive, send)
        finally:
            request_id.reset(token)
