"""What a refused request answers: one envelope, and the codes it can name.

Every failure this API produces comes back as

    {"error": {"code": "SESSION_INVALID", "message": "Invalid or expired session"}}

The two halves are for two different readers. ``code`` is for the program: a
closed set, stable across wording changes, safe to branch on. ``message`` is for
whoever ends up reading it, and is the one part free to be reworded — so a
client that branches on the sentence is a client that breaks on a typo fix.

The split is why the code is not simply the status. A status says what class of
thing went wrong and a caller usually has to act on which one: ``/auth/callback``
answers 502 both for a provider that could not be reached and for one requiring a
client authentication method this backend does not implement, and only the second
is worth a bug report rather than a retry. It runs the other way too — the same
route answers one sentence, "Token exchange failed", under both 400 and 502,
because repeating a provider's reasons back says more about that server than a
patient asked. The code is what tells them apart without saying any of it.

Routes raise ``APIError``. The handlers below render it, and render everything
else that can refuse a request — a validation failure, a 404 from the router, an
``HTTPException`` from inside FastAPI — into the same envelope, so the shape does
not depend on which layer decided to say no.
"""

from __future__ import annotations

from enum import Enum

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(str, Enum):
    """The closed set of reasons this API refuses a request.

    A ``str`` enum so it serializes as the bare code and appears in the OpenAPI
    document as an enumerated schema, which is what lets a generated client
    switch on it exhaustively.
    """

    # --- the request itself ---------------------------------------------------
    INVALID_REQUEST = "INVALID_REQUEST"
    UNKNOWN_PROVIDER = "UNKNOWN_PROVIDER"
    ISSUER_NOT_ALLOWED = "ISSUER_NOT_ALLOWED"
    INVALID_STATE = "INVALID_STATE"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"

    # --- who is asking --------------------------------------------------------
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    SESSION_INVALID = "SESSION_INVALID"
    SIGN_IN_NOT_VERIFIED = "SIGN_IN_NOT_VERIFIED"

    # --- what they asked for --------------------------------------------------
    RECORD_NOT_FOUND = "RECORD_NOT_FOUND"
    CONNECTION_NOT_FOUND = "CONNECTION_NOT_FOUND"

    # --- the authorization flow -----------------------------------------------
    AUTHORIZATION_REJECTED = "AUTHORIZATION_REJECTED"
    NO_PATIENT_CONTEXT = "NO_PATIENT_CONTEXT"
    REAUTHORIZATION_REQUIRED = "REAUTHORIZATION_REQUIRED"

    # --- something upstream ---------------------------------------------------
    PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"
    PROVIDER_UNSUPPORTED = "PROVIDER_UNSUPPORTED"
    IDENTITY_PROVIDER_ERROR = "IDENTITY_PROVIDER_ERROR"
    CONNECTION_UNREADABLE = "CONNECTION_UNREADABLE"

    # --- this deployment ------------------------------------------------------
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    LOGIN_NOT_CONFIGURED = "LOGIN_NOT_CONFIGURED"
    ENDPOINT_DATA_UNAVAILABLE = "ENDPOINT_DATA_UNAVAILABLE"

    # --- us -------------------------------------------------------------------
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# What each code means, in one line. This is the documentation: it is what the
# OpenAPI examples and the README table are built from, so a code cannot be added
# without saying what it is for, and the explanation cannot drift from the set.
MEANINGS: dict[ErrorCode, str] = {
    ErrorCode.INVALID_REQUEST: (
        "The request could not be read, or carried a value this API will not act on."
    ),
    ErrorCode.UNKNOWN_PROVIDER: (
        "No provider is registered under that key in this deployment."
    ),
    ErrorCode.ISSUER_NOT_ALLOWED: (
        "The issuer is not one the named provider is registered for, or not one "
        "this backend will fetch at all."
    ),
    ErrorCode.INVALID_STATE: (
        "The `state` does not match an authorization in flight, has expired, or "
        "has already been used."
    ),
    ErrorCode.NOT_FOUND: "There is no such route.",
    ErrorCode.METHOD_NOT_ALLOWED: "That route does not accept this method.",
    ErrorCode.AUTHENTICATION_REQUIRED: (
        "No session was presented, and this request needs one."
    ),
    ErrorCode.SESSION_INVALID: (
        "The session presented is unknown, expired, or no longer resolves."
    ),
    ErrorCode.SIGN_IN_NOT_VERIFIED: (
        "The identity provider's answer could not be verified, so no account was "
        "signed in."
    ),
    ErrorCode.RECORD_NOT_FOUND: (
        "There is no such patient record, or the session presented does not hold it."
    ),
    ErrorCode.CONNECTION_NOT_FOUND: (
        "That provider is not connected to this record."
    ),
    ErrorCode.AUTHORIZATION_REJECTED: (
        "The provider refused the authorization code — it was wrong, already "
        "spent, or expired."
    ),
    ErrorCode.NO_PATIENT_CONTEXT: (
        "The authorization succeeded but named no patient, so there is nothing to "
        "hang a connection off."
    ),
    ErrorCode.REAUTHORIZATION_REQUIRED: (
        "The stored authorization is no longer good; this provider has to be "
        "connected again."
    ),
    ErrorCode.PROVIDER_UNREACHABLE: (
        "The EHR server could not be reached, or did not answer with a usable "
        "SMART configuration."
    ),
    ErrorCode.PROVIDER_UNSUPPORTED: (
        "The EHR server requires something this backend does not implement, such "
        "as an unsupported client authentication method."
    ),
    ErrorCode.IDENTITY_PROVIDER_ERROR: (
        "The identity provider could not be reached, or did not answer usably."
    ),
    ErrorCode.CONNECTION_UNREADABLE: (
        "A stored connection could not be read. This is a server-side fault "
        "rather than anything the caller did."
    ),
    ErrorCode.PROVIDER_NOT_CONFIGURED: (
        "That provider has no credentials in this deployment, so it cannot be "
        "authorized against."
    ),
    ErrorCode.LOGIN_NOT_CONFIGURED: (
        "This deployment has no identity provider configured, so there is nothing "
        "to sign in to."
    ),
    ErrorCode.ENDPOINT_DATA_UNAVAILABLE: (
        "No endpoint data is available to serve just now."
    ),
    ErrorCode.RATE_LIMITED: (
        "Too many requests from this client. `Retry-After` says when to come back."
    ),
    ErrorCode.INTERNAL_ERROR: (
        "Something failed in a way this API does not model. The `x-request-id` "
        "header is what ties the response to the log line."
    ),
}


class ErrorDetail(BaseModel):
    """Why a request was refused: a code to branch on, a sentence to read."""

    code: ErrorCode
    message: str


class ErrorResponse(BaseModel):
    """The body of every failed request.

    Nested under ``error`` rather than flat, so a caller can tell a refusal from
    a success by one key and without consulting the status — and so a success
    shape that happens to carry a ``code`` field of its own cannot be mistaken
    for one.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": ErrorCode.SESSION_INVALID.value,
                        "message": "Invalid or expired session",
                    }
                }
            ]
        }
    )

    error: ErrorDetail


class APIError(HTTPException):
    """A refusal, raised from a route, carrying the code it answers under.

    Subclasses ``HTTPException`` rather than replacing it so everything already
    built around one keeps working: FastAPI unwinds it inside the middleware
    stack (which is what keeps the CORS headers on it — see
    ``app/api/middleware.py``), and ``detail`` still carries the message for the
    handful of places that catch a refusal and re-render it.
    """

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code

    @property
    def message(self) -> str:
        return str(self.detail)


# The code to answer under when something other than a route refused the request:
# FastAPI's own 404 for an unrouted path, a 405 from the router, an
# HTTPException raised by a dependency this module knows nothing about. Narrow on
# purpose — anything a route refuses deliberately names its own code.
_BY_STATUS: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.AUTHENTICATION_REQUIRED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
}


def code_for_status(status_code: int) -> ErrorCode:
    """The code to use where none was named: the mapped one, else by class.

    A 5xx nobody modelled is ours to own and reads as ``INTERNAL_ERROR``; a 4xx
    nobody modelled is something about the request. Neither is a case worth
    inventing a code for, and both are cases where a caller still gets a code.
    """
    if status_code in _BY_STATUS:
        return _BY_STATUS[status_code]
    return ErrorCode.INTERNAL_ERROR if status_code >= 500 else ErrorCode.INVALID_REQUEST


def error_body(code: ErrorCode, message: str) -> dict:
    """The envelope, as a plain dict, built through the model that declares it.

    Every response in this module goes through here, so there is one definition
    of the shape and no path that can quietly answer a different one.
    """
    return ErrorResponse(error=ErrorDetail(code=code, message=message)).model_dump(
        mode="json"
    )


def error_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """A refusal as a response, for the few places that return one rather than raise."""
    return JSONResponse(
        error_body(code, message), status_code=status_code, headers=headers
    )


def refusal(description: str, *codes: ErrorCode) -> dict:
    """One entry for a route's ``responses``, documenting why it was refused.

    Every refusal this API can answer has the same body, so the only thing that
    varies per status is the prose and which codes can appear under it. Naming
    the codes here is what puts them in the OpenAPI document per endpoint — a
    caller reading ``/docs`` sees the exact set a given status can carry, rather
    than the whole enum and a guess.
    """
    entry: dict = {"model": ErrorResponse, "description": description}
    if codes:
        entry["content"] = {
            "application/json": {
                "examples": {
                    code.value: {
                        "summary": code.value,
                        "description": MEANINGS[code],
                        "value": error_body(code, MEANINGS[code]),
                    }
                    for code in codes
                }
            }
        }
    return entry


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Render any refusal — ours or the framework's — as the one envelope.

    Registered for Starlette's ``HTTPException`` rather than FastAPI's, because
    the router raises the base class for an unrouted path and a bad method, and
    those are refusals a caller sees as readily as any other.
    """
    code = exc.code if isinstance(exc, APIError) else code_for_status(exc.status_code)
    message = exc.detail if isinstance(exc.detail, str) else MEANINGS[code]
    return error_response(exc.status_code, code, message, headers=exc.headers)


def _readable(exc: RequestValidationError) -> str:
    """A validation failure as a sentence, naming fields and never their values.

    FastAPI's own 422 body lists each error with the ``input`` that caused it,
    and on this API an input is a patient's identifier as often as it is a page
    number. The location and the complaint are enough to fix a request and carry
    nothing that was sent.
    """
    parts = []
    for error in exc.errors():
        # The whole location, "body.iss" or "query.pageSize", which is what names
        # the field at fault in a way a caller can act on.
        location = ".".join(str(piece) for piece in error.get("loc", ()))
        complaint = str(error.get("msg", "invalid"))
        parts.append(f"{location}: {complaint}" if location else complaint)
    if not parts:
        return "Request validation failed"
    return "Request validation failed — " + "; ".join(parts)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Answer a malformed request in the same envelope as every other refusal.

    Still a 422, which is the status FastAPI has always answered here and the one
    a caller's existing handling is written against; what changes is the body,
    which used to be the only shape in the API that was a list.
    """
    return error_response(422, ErrorCode.INVALID_REQUEST, _readable(exc))


# What FastAPI generates for a 422 describes its own ``HTTPValidationError``,
# which this no longer answers with. Declared here so ``app/main.py`` can put the
# real shape in the document, next to the handler that produces it.
VALIDATION_FAILED = refusal(
    "The request's shape or parameters are not valid.", ErrorCode.INVALID_REQUEST
)

# True of every route, since it is the middleware and not any one of them that
# answers it. Applied across the document in ``app/main.py`` rather than restated
# in each router's ``responses``.
UNEXPECTED_FAILURE = refusal(
    "Something failed in a way this API does not model.", ErrorCode.INTERNAL_ERROR
)
