"""Signing a user in: the OIDC authorization code flow, both halves, plus out.

The authentication layer, as distinct from the authorization one next door in
``app/api/auth.py``. That one answers "may this application read your chart at
this hospital"; this one answers "who are you". Before it existed the second
question had no answer at all: a session was minted by connecting a provider, so
the closest thing to an identity was possession of a token, and a record was
reachable only until that session expired.

What signing in buys is in ``User.patient_id`` — the record stops belonging to a
session and starts belonging to a person, so it is still there on the next visit.

No ``from __future__ import annotations`` here, for the reason spelled out in
``app/api/auth.py``: the rate limiter's wrapper carries slowapi's globals, so
FastAPI cannot resolve a stringified annotation back to the type it names.
"""

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    RATE_LIMITED,
    Principal,
    auth_rate_limit,
    get_current_principal,
    limiter,
)
from app.api.schemas import (
    AuthProviderInfo,
    LoginCallbackResponse,
    LoginResponse,
    LogoutResponse,
    SessionResponse,
    UserProfile,
    refusal,
)
from app.auth import oidc
from app.auth.models import AppSession, LoginState, utcnow
from app.core.config import get_settings
from app.core.db import (
    delete_expired_login_states,
    delete_expired_sessions,
    get_session,
    upsert_user,
)
from app.core.logging import fields

logger = logging.getLogger(__name__)

router = APIRouter(tags=["authentication"])

NOT_CONFIGURED = refusal(
    "This deployment has no identity provider configured, so there is nothing "
    "to sign in to"
)


class LoginCallbackData(BaseModel):
    code: str
    state: str


def _client() -> oidc.OIDCClient:
    """The configured IdP client, as a 503 where none is configured.

    A deployment that set no ``OIDC_ISSUER`` has not failed at signing someone
    in; it does not offer signing in. 503 says that, and says it the same way
    ``/auth/connect`` already answers for a provider with no credentials.
    """
    try:
        return oidc.client()
    except oidc.OIDCNotConfigured as exc:
        raise HTTPException(
            status_code=503, detail="Login is not configured for this deployment"
        ) from exc


def _log_login_failure(exc: Exception, *, stage: str) -> None:
    """Name which part of a sign-in failed, without changing what the user is told.

    The same split as the SMART path makes: the wording that helps someone
    signing in is not the wording that helps whoever has to fix the IdP
    registration, and a misconfigured ``aud`` or a clock an hour out both read as
    "could not sign you in" to the person in front of it. The exception's own
    message is not written — it quotes claims.
    """
    logger.warning(
        "Login %s failed: %s",
        stage,
        type(exc).__name__,
        **fields(
            event=f"login.{stage}.failed",
            reason=type(exc).__name__,
            exception=type(exc).__name__,
        ),
    )


def _profile(user) -> UserProfile:
    """The account, as the API reports it.

    One definition, used by every route that returns a user, so the three cannot
    come to describe the same account differently.
    """
    return UserProfile(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        patient_id=user.patient_id,
    )


@router.get(
    "/auth/providers",
    response_model=AuthProviderInfo,
    summary="Whether this deployment can sign users in",
)
async def auth_provider_info():
    """Report whether login is available, so a frontend can render accordingly.

    Unauthenticated and unthrottled: it is the call a frontend makes before it
    knows whether to show a sign-in button, and it discloses only what the
    deployment's own configuration already implies.
    """
    settings = get_settings()
    enabled = settings.user_login_configured
    return AuthProviderInfo(
        login_enabled=enabled,
        login_required=settings.require_user_login,
        # Only where login is on. Naming an issuer a deployment half-configured
        # would advertise a sign-in that cannot work.
        issuer=settings.oidc_issuer if enabled else None,
    )


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    summary="Start signing a user in",
    responses={
        429: RATE_LIMITED,
        502: refusal("The identity provider's configuration could not be read"),
        503: NOT_CONFIGURED,
    },
)
@limiter.limit(auth_rate_limit)
async def login(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Begin the OIDC flow and return where to send the user to sign in.

    Answers with the authorization URL rather than redirecting to it, matching
    `POST /auth/connect`, so the frontend keeps control of the navigation and a
    caller that is not a browser can drive the flow.

    Send the user there; the identity provider redirects back to this
    application's registered `redirect_uri` with a `code` and the `state`
    returned here, which `POST /auth/login/callback` exchanges for a session.
    """
    client = _client()
    try:
        config = await client.discover()
    except oidc.OIDCDiscoveryError as exc:
        _log_login_failure(exc, stage="discovery")
        raise HTTPException(
            status_code=502,
            detail="Could not read the identity provider's configuration",
        ) from exc

    # Opportunistic sweep so abandoned logins do not accumulate, on the same
    # reasoning as the one in /auth/connect: starting a login is what creates
    # these rows, so the cleanup runs exactly as often as the growth does.
    await delete_expired_login_states(session)

    ttl = get_settings().login_state_ttl_seconds
    state = LoginState.issue(ttl)
    url = client.build_login_url(
        config,
        state=state.state,
        nonce=state.nonce,
        code_verifier=state.code_verifier,
    )
    session.add(state)
    await session.commit()

    return LoginResponse(authorization_url=url, state=state.state, expires_in=ttl)


@router.post(
    "/auth/login/callback",
    response_model=LoginCallbackResponse,
    summary="Exchange the sign-in code for a session",
    responses={
        400: refusal("Invalid or expired state, or an identity that did not verify"),
        429: RATE_LIMITED,
        502: refusal("The identity provider could not be reached"),
        503: NOT_CONFIGURED,
    },
)
@limiter.limit(auth_rate_limit)
async def login_callback(
    request: Request,
    body: LoginCallbackData = Body(
        openapi_examples={
            "default": {
                "summary": "Complete a sign-in",
                "value": {
                    "code": "C_xY2n8Qb7hRc1TvWyLp3",
                    "state": "Yl8kQq2Xb7nHc1RrTv9WdF3s",
                },
            }
        }
    ),
    session: AsyncSession = Depends(get_session),
):
    """Complete a sign-in and return the session that represents it.

    Called with the `code` and `state` the identity provider redirected back
    with. What comes back is the account and a session to present on every
    authenticated route — including `POST /auth/connect`, where it is what makes
    a new connection join the record this account already owns.

    A first sign-in creates the account. A later one finds it, and with it the
    patient record and every provider on it.
    """
    client = _client()

    state_row = await session.get(LoginState, body.state)
    if state_row is None or state_row.is_expired:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    nonce = state_row.nonce
    code_verifier = state_row.code_verifier
    # Consume the state so the same sign-in cannot be replayed within its TTL.
    await session.delete(state_row)
    await session.commit()

    try:
        config = await client.discover()
        token_response = await client.exchange_code(config, body.code, code_verifier)
    except oidc.OIDCDiscoveryError as exc:
        _log_login_failure(exc, stage="discovery")
        raise HTTPException(
            status_code=502,
            detail="Could not read the identity provider's configuration",
        ) from exc
    except oidc.OIDCExchangeError as exc:
        # The provider refused the code, or could not be reached to ask. Both are
        # 502 rather than 400: the state was ours and it was good, so whatever
        # went wrong is between us and the provider, not the user's doing.
        _log_login_failure(exc, stage="token_exchange")
        raise HTTPException(
            status_code=502, detail="Could not complete sign-in with the identity provider"
        ) from exc

    try:
        identity = await client.identity_from(config, token_response, nonce=nonce)
    except oidc.OIDCDiscoveryError as exc:
        _log_login_failure(exc, stage="signing_keys")
        raise HTTPException(
            status_code=502,
            detail="Could not read the identity provider's signing keys",
        ) from exc
    except oidc.IDTokenError as exc:
        # A token that came back from a successful exchange and does not verify.
        # Refused outright: this is the check that stands between an assertion
        # about who someone is and an account holding their medical records.
        _log_login_failure(exc, stage="id_token")
        raise HTTPException(status_code=400, detail="Sign-in could not be verified") from exc

    user = await upsert_user(session, identity)
    ttl = get_settings().app_session_ttl_seconds
    app_session = AppSession.for_user(user_id=user.id, ttl_seconds=ttl)
    session.add(app_session)
    # Folded into the same commit, for the same reason /auth/callback folds its
    # sweeps in: signing in is an event that creates session rows, so it is the
    # right cadence at which to retire them, and it needs no scheduler.
    await delete_expired_sessions(session)
    await session.commit()

    logger.info(
        "Signed in",
        **fields(event="login.succeeded", user_id=user.id, iss=identity.issuer),
    )

    return LoginCallbackResponse(
        session_id=app_session.session_id,
        expires_in=ttl,
        user=_profile(user),
    )


@router.get(
    "/auth/me",
    response_model=SessionResponse,
    summary="Who the presented session belongs to",
    responses={401: refusal("Missing, invalid, or expired session")},
)
async def current_session(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
):
    """Report the account and record behind the bearer, for a frontend resuming.

    This is what a page reload calls: it is the difference between a stored
    bearer that still works and one that does not, and it is where a returning
    user learns the record id to read — which after signing in they otherwise
    have no way to know, since the authorization that produced it may have been
    weeks ago.

    `patientId` is null for someone signed in who has connected nothing yet.
    """
    remaining = int((principal.session.expires_at - utcnow()).total_seconds())
    return SessionResponse(
        authenticated=principal.is_authenticated,
        patient_id=principal.patient_id,
        # The guard already refused an expired session, but the two reads are a
        # moment apart and the subtraction must not report a negative age for a
        # session that lapsed in between.
        expires_in=max(remaining, 0),
        user=_profile(principal.user) if principal.user else None,
    )


@router.post(
    "/auth/logout",
    response_model=LogoutResponse,
    summary="Sign out, ending this session",
    responses={401: refusal("Missing, invalid, or expired session")},
)
async def logout(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
):
    """End the presented session, and say where to end it at the provider too.

    Only this session. Signing out on one device does not sign the account out
    everywhere, which is the behaviour a user expects and the one that does not
    surprise someone mid-read on their phone.

    The stored connections are deliberately untouched: signing out is not
    disconnecting, and the record is still there at the next sign-in. Ending a
    provider's access is `DELETE /patients/{patientId}/connections/{provider}`,
    which revokes at the EHR.
    """
    await session.delete(principal.session)
    await session.commit()

    logger.info(
        "Signed out",
        **fields(event="logout.succeeded", user_id=principal.session.user_id),
    )

    # Best effort, and never at the cost of the logout itself: the session is
    # already gone by the time this runs, so an IdP that cannot be reached can
    # only cost the caller the extra URL, not their sign-out.
    logout_url = None
    if get_settings().user_login_configured and principal.is_authenticated:
        try:
            client = oidc.client()
            logout_url = client.logout_url(await client.discover())
        except oidc.OIDCError as exc:
            _log_login_failure(exc, stage="logout_url")

    return LogoutResponse(success=True, logout_url=logout_url)
