"""What every router needs: the session guard and the throttle.

These live in one module rather than in ``main`` so the routers can import them
without importing the application, which would be circular. The single shared
``limiter`` matters in particular: slowapi's decorator binds to the instance it
is applied with, so a second ``Limiter()`` would hand each router its own bucket
and quietly double the effective limit.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import refusal
from app.auth.models import AppSession, User
from app.core.config import get_settings
from app.core.db import get_session, user_for_session

# Throttle per client IP. The auth endpoints trigger outbound discovery and DB
# writes, and the resource endpoints fan out to many FHIR calls, so both are
# abuse-amplification surfaces worth capping. Limits are read from settings at
# request time so they can be tuned without touching this file.
#
# get_remote_address keys on the socket peer, which is correct for a direct
# connection. Behind a reverse proxy or load balancer that becomes the proxy's
# IP (one shared bucket for everyone), so a proxied deployment must add trusted
# X-Forwarded-For handling (e.g. Starlette's ProxyHeadersMiddleware) and key on
# the forwarded client address.
limiter = Limiter(key_func=get_remote_address, enabled=get_settings().rate_limit_enabled)


def auth_rate_limit() -> str:
    return get_settings().auth_rate_limit


def fhir_rate_limit() -> str:
    return get_settings().fhir_rate_limit


def endpoint_check_rate_limit() -> str:
    return get_settings().endpoint_check_rate_limit


# What a throttled route answers, for the OpenAPI document. Declared beside the
# limiter so a route that adds the decorator has the matching documentation to
# hand, rather than reaching into another router for it.
RATE_LIMITED = refusal(
    "Too many requests from this client. `Retry-After` says when to come back."
)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """Add ``Retry-After`` to a throttled request's refusal.

    This exists for the header, not for the body. ``RateLimitExceeded`` is
    already an ``HTTPException``, so FastAPI would render it as ``{"detail": …}``
    on its own; it is slowapi's *handler* that answers ``{"error": …}``, and
    registering this one in its place is what keeps 429 from being the single
    status a caller has to special-case. Rendering is delegated back to FastAPI
    rather than rebuilt, so there is one definition of what a refusal looks like.

    The window comes from the limit itself rather than from slowapi's
    ``headers_enabled``. That flag also injects headers into *successful*
    responses, which it does by reaching for a ``response`` argument on the
    endpoint — so every throttled route would need a parameter it has no other
    use for, and one answering a Pydantic model raises on the way out instead of
    returning. A full window is conservative: coming back early is the failure a
    caller can absorb.
    """
    return await http_exception_handler(
        request,
        HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {exc.detail}",
            headers={"Retry-After": str(exc.limit.limit.get_expiry())},
        ),
    )


# Resource access is gated on the app session bearer issued at /auth/callback.
# auto_error=False so a missing or malformed header yields None and we answer 401
# ourselves — FastAPI's built-in default for a missing bearer is 403.
session_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """Who is making this request, and which patient record they reach.

    A bearer resolves to one of two things, and every protected route wants the
    answer to the same question about either — so the question is answered once,
    here, rather than at each of them.

    The record is *derived* rather than read off the session, because for a
    signed-in caller it is a property of the account and can change during the
    session's life: someone who signs in and then connects their first provider
    is holding a bearer that was minted before their record existed. Reading
    through the account means that bearer reaches the record as soon as there is
    one. For a caller who never signed in there is no account, and the session's
    own ``patient_id`` is the only record there is.

    ``patient_id`` is None for a signed-in caller who has connected nothing yet.
    That is a real state and not an error: they are authenticated, with no record
    to read.
    """

    session: AppSession
    user: User | None

    @property
    def patient_id(self) -> str | None:
        """The record this caller reaches, or None where they have none yet."""
        if self.user is not None:
            return self.user.patient_id
        return self.session.patient_id

    @property
    def is_authenticated(self) -> bool:
        """Whether a person signed in, as opposed to merely holding a session."""
        return self.user is not None


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(session_bearer),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Resolve and validate the caller behind the request's bearer token.

    The single authorization path for protected endpoints: a missing/malformed
    header or an unknown/expired session is a 401 (sign in or authorize again).
    What comes back names the record the caller reaches, never one taken from
    the request.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing session credentials")
    return await _resolve_principal(credentials, session)


async def get_optional_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(session_bearer),
    session: AsyncSession = Depends(get_session),
) -> Principal | None:
    """The same guard for an endpoint where the bearer is optional.

    Used by ``/auth/connect``, where presenting a session means "this new
    connection belongs to me". Sending no bearer is a valid request; sending a
    bad one is not, and is rejected rather than quietly treated as if none had
    been sent — otherwise an expired session would silently start a second,
    unlinked patient record.
    """
    if credentials is None:
        return None
    return await _resolve_principal(credentials, session)


async def _resolve_principal(
    credentials: HTTPAuthorizationCredentials, session: AsyncSession
) -> Principal:
    app_session = await session.get(AppSession, credentials.credentials)
    if app_session is None or app_session.is_expired:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    user = await user_for_session(session, app_session)
    if app_session.user_id is not None and user is None:
        # The session names an account that is no longer there. Nothing should
        # produce this — accounts are not deleted — but resolving it as an
        # anonymous session would hand the bearer whatever patient_id happened to
        # be frozen on it, which is the one outcome worth refusing outright.
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    return Principal(session=app_session, user=user)
