"""What every router needs: the session guard, the throttle, and the provider factory.

These live in one module rather than in ``main`` so the routers can import them
without importing the application, which would be circular. The single shared
``limiter`` matters in particular: slowapi's decorator binds to the instance it
is applied with, so a second ``Limiter()`` would hand each router its own bucket
and quietly double the effective limit.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import refusal
from app.auth.models import AppSession
from app.core.config import get_settings
from app.core.db import get_session
from app.providers.discovery import SMARTDiscovery
from app.providers.generic import GenericSMARTProvider

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


# One discovery cache shared across requests: re-authorizing the same issuer
# reuses its SMART configuration instead of re-fetching it each time.
discovery = SMARTDiscovery()


def provider_for(ehr: dict, iss: str) -> GenericSMARTProvider:
    """Build the discovery-driven provider for one provider/issuer connection.

    The token is bound to the issuer the app intends to call, so aud is the
    issuer, never a hardcoded server.
    """
    return GenericSMARTProvider(
        client_id=ehr["client_id"],
        client_secret=ehr.get("client_secret"),
        redirect_uri=ehr["redirect_uri"],
        aud=iss.rstrip("/"),
        discovery=discovery,
    )


# Resource access is gated on the app session bearer issued at /auth/callback.
# auto_error=False so a missing or malformed header yields None and we answer 401
# ourselves — FastAPI's built-in default for a missing bearer is 403.
session_bearer = HTTPBearer(auto_error=False)


async def get_current_session(
    credentials: HTTPAuthorizationCredentials | None = Depends(session_bearer),
    session: AsyncSession = Depends(get_session),
) -> AppSession:
    """Resolve and validate the app session behind the request's bearer token.

    The single authorization path for protected endpoints: a missing/malformed
    header or an unknown/expired session is a 401 (authorize again). Returning
    the row means the caller reaches only the patient it names, never one from
    the request.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing session credentials")
    return await _resolve_session(credentials, session)


async def get_optional_session(
    credentials: HTTPAuthorizationCredentials | None = Depends(session_bearer),
    session: AsyncSession = Depends(get_session),
) -> AppSession | None:
    """The same guard for an endpoint where the bearer is optional.

    Used by ``/auth/connect``, where presenting a session means "this new
    connection belongs to the patient record I already hold". Sending no bearer
    is a valid request; sending a bad one is not, and is rejected rather than
    quietly treated as if none had been sent — otherwise an expired session
    would silently start a second, unlinked patient record.
    """
    if credentials is None:
        return None
    return await _resolve_session(credentials, session)


async def _resolve_session(
    credentials: HTTPAuthorizationCredentials, session: AsyncSession
) -> AppSession:
    app_session = await session.get(AppSession, credentials.credentials)
    if app_session is None or app_session.is_expired:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return app_session
