"""Authorizing a patient at an EHR: the SMART App Launch flow, both halves.

Deliberately no ``from __future__ import annotations`` here. The rate limiter
wraps each endpoint with ``functools.wraps``, which copies a function's name and
docstring but cannot copy its ``__globals__``; FastAPI resolves annotations
against that namespace, so a stringified annotation would be looked up in
slowapi's module rather than this one. The failure is quiet and confusing: the
request body model becomes unresolvable and FastAPI falls back to reading it as a
query parameter. Real annotation objects sidestep it, and the union syntax this
module uses needs no future import on the supported Python versions.
"""

import logging
import secrets

import httpx
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    RATE_LIMITED,
    Principal,
    auth_rate_limit,
    get_optional_principal,
    limiter,
)
from app.api.errors import APIError, ErrorCode, error_response, refusal
from app.api.schemas import (
    CallbackResponse,
    Connection,
    ConnectRequest,
    ConnectResponse,
)
from app.auth.models import AppSession, OAuthState, User
from app.core.config import get_settings
from app.core.db import (
    delete_dead_connections,
    delete_expired_sessions,
    delete_expired_states,
    get_session,
    persist_token,
)
from app.core.logging import fields
from app.providers import config, discovery, scopes
from app.providers.discovery import SMARTDiscoveryError
from app.providers.generic import SMARTProviderError, TokenExchangeError
from app.providers.registry import provider_for

logger = logging.getLogger(__name__)

router = APIRouter(tags=["authorization"])


def _upstream_reason(exc: Exception) -> tuple[str, int | None]:
    """What this failure was, and what the server answered if it answered at all.

    A discovery failure is named by ``discovery.failure_status``, the same
    function `GET /providers/endpoint-check` answers callers with, so a log line
    and a pre-flight check describe the same server the same way. It also folds
    two situations into ``unreachable`` that read alike here and not at all alike
    to whoever has to act on them — a server that refused, and one that never
    answered — which is what the status beside the reason is for.
    """
    if isinstance(exc, SMARTDiscoveryError):
        return discovery.failure_status(exc), exc.status_code
    if isinstance(exc, TokenExchangeError):
        # RFC 6749 §5.2 spends a distinct code on each way an exchange can be
        # refused; a server that sent none has still refused it.
        return exc.oauth_error or "rejected", exc.status_code
    if isinstance(exc, SMARTProviderError):
        return "unsupported_client_authentication", None
    return "unreachable", None


def _log_upstream_failure(exc: Exception, *, stage: str, provider: str, iss: str) -> None:
    """Name which upstream failure a refusal was, without changing the refusal.

    All of these answer a caller the same sentence, deliberately: the wording that
    helps a patient is not the wording that helps whoever has to fix it, and
    naming a server's fault back to the browser tells anyone probing issuers what
    they found. So the distinction is kept here, where it used to be discarded — a
    connect that failed left nothing behind saying which of four things had
    happened, and diagnosing one meant reproducing it by hand.

    The exception's own message is not written. It carries the URL fetched and the
    parser's complaint, and a complaint quotes what it choked on.
    """
    reason, status = _upstream_reason(exc)
    logger.warning(
        "Upstream %s failed for %s: %s",
        stage,
        provider,
        reason,
        **fields(
            event=f"auth.{stage}.failed",
            provider=provider,
            iss=iss,
            reason=reason,
            status=status,
            exception=type(exc).__name__,
        ),
    )


def _log_scope_narrowing(provider: str, granted: str | None) -> None:
    """Say so once, here, when a grant covers less of the record than was asked for.

    A narrowing is not an error and does not change what the authorization
    returns: the connection works, for part of the record. But it is the sole
    explanation for reads that come back withheld from then on, and this is the
    one moment it is known — after that there is only a stored scope nobody
    re-reads.

    Measured against the default tier, which is what a read of this connection
    will actually ask for, rather than against the scopes requested: the request
    is a wildcard for most providers, and "the server granted less than *" names
    nothing an operator can act on.
    """
    withheld = scopes.unreadable(
        config.fhir_types_for(config.ResourceTier.US_CORE), granted
    )
    if not withheld:
        return
    logger.info(
        "%s granted a narrower scope than requested; %d resource type(s) withheld",
        provider,
        len(withheld),
        **fields(
            event="auth.scope.narrowed",
            provider=provider,
            withheld=",".join(withheld),
        ),
    )


class CallbackData(BaseModel):
    code: str
    state: str


async def _begin_authorization(
    session: AsyncSession,
    *,
    provider: str,
    iss: str,
    link_patient_id: str | None,
    user_id: str | None = None,
) -> tuple[str, str, int]:
    """Validate a connection request and mint the authorization it needs.

    Returns ``(authorization_url, state, ttl_seconds)``. Everything that can be
    refused is refused here, before the patient is sent anywhere, and as an
    ``APIError`` so a caller is told which of the two it was: their request, or
    the server they named.
    """
    ehr = config.EHR_CONFIGS.get(provider)
    if not ehr:
        raise APIError(
            400, ErrorCode.UNKNOWN_PROVIDER, "Unknown or unsupported provider"
        )

    # A provider with no configured client_id is a deployment that did not set
    # its credentials; fail clearly here rather than redirect the user to the
    # EHR with an empty client_id and let them hit an opaque invalid_client.
    if not ehr.get("client_id"):
        raise APIError(
            503, ErrorCode.PROVIDER_NOT_CONFIGURED, "Provider is not configured"
        )

    # Canonicalize once so the allowlist check, the persisted state, and the
    # derived aud all agree on the same issuer string.
    iss = iss.rstrip("/")

    # Only authorize against an issuer this provider was registered with. This
    # runs before any discovery request, so a caller cannot point us at an
    # arbitrary server to probe it (SSRF) or to receive our client secret.
    # Exact match is the default; a provider may also opt into host-scoped prefix
    # matching (the SMART launcher encodes standalone launch context in the aud
    # path, so its FHIR base varies under a fixed prefix).
    allowed_issuers = {a.rstrip("/") for a in ehr.get("allowed_issuers", [])}
    allowed_prefixes = ehr.get("allowed_issuer_prefixes", [])
    if iss not in allowed_issuers and not any(
        iss.startswith(prefix) for prefix in allowed_prefixes
    ):
        raise APIError(
            400, ErrorCode.ISSUER_NOT_ALLOWED, "Issuer not allowed for this provider"
        )

    provider_adapter = provider_for(ehr, iss)
    try:
        smart_config = await provider_adapter.discover(iss)
    except SMARTDiscoveryError as exc:
        _log_upstream_failure(exc, stage="discovery", provider=provider, iss=iss)
        raise APIError(
            502,
            ErrorCode.PROVIDER_UNREACHABLE,
            "Could not read the server's SMART configuration",
        ) from exc

    # Opportunistic sweep so expired anti-CSRF state does not accumulate.
    await delete_expired_states(session)

    ttl = get_settings().oauth_state_ttl_seconds
    state = secrets.token_urlsafe(16)
    auth = provider_adapter.build_auth_url(smart_config, state, ehr["scopes"].split())
    session.add(
        OAuthState.issue(
            state,
            iss,
            provider,
            ttl,
            code_verifier=auth.code_verifier,
            link_patient_id=link_patient_id,
            user_id=user_id,
        )
    )
    await session.commit()

    return auth.url, state, ttl


@router.post(
    "/auth/connect",
    response_model=ConnectResponse,
    summary="Start connecting a patient to a provider",
    responses={
        400: refusal(
            "Unknown provider, or an issuer that provider is not registered for",
            ErrorCode.UNKNOWN_PROVIDER,
            ErrorCode.ISSUER_NOT_ALLOWED,
        ),
        401: refusal(
            "A session was presented but is invalid or expired, or this deployment "
            "requires signing in and no session was presented",
            ErrorCode.AUTHENTICATION_REQUIRED,
            ErrorCode.SESSION_INVALID,
        ),
        429: RATE_LIMITED,
        502: refusal(
            "The server's SMART configuration could not be read",
            ErrorCode.PROVIDER_UNREACHABLE,
        ),
        503: refusal(
            "The provider has no credentials configured in this deployment",
            ErrorCode.PROVIDER_NOT_CONFIGURED,
        ),
    },
)
@limiter.limit(auth_rate_limit)
async def connect(
    request: Request,
    body: ConnectRequest = Body(
        openapi_examples={
            "first": {
                "summary": "Connect a patient's first provider",
                "value": {
                    "provider": "EPIC_SANDBOX",
                    "iss": "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
                },
            },
            "second": {
                "summary": "Add a second provider to the same record",
                "description": "Send a session as `Authorization: Bearer <sessionId>` "
                "— from signing in, or from the first connection. The new connection "
                "joins that patient record instead of starting one of its own.",
                "value": {
                    "provider": "CERNER_SANDBOX",
                    "iss": "https://fhir-ehr-code.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d",
                },
            },
        }
    ),
    principal: Principal | None = Depends(get_optional_principal),
    session: AsyncSession = Depends(get_session),
):
    """Begin the SMART App Launch flow and return where to send the patient.

    Answers with the authorization URL rather than redirecting to it, so a caller
    that is not a browser can drive the flow. Send the patient there; the
    provider redirects back to this application's registered `redirect_uri` with
    a `code` and the `state` returned here, which `POST /auth/callback` exchanges
    for tokens.

    Presenting an existing session means "this provider is the same patient as
    the record I already hold", and the resulting connection joins that record.
    A session from signing in says so for the account rather than for one
    connection, which is what lets a provider be added weeks later. Without a
    session the connection anchors a new record — unless this deployment sets
    `REQUIRE_USER_LOGIN`, in which case there is no anonymous path and the
    request is refused.
    """
    # Refused before anything is done, so a deployment that requires an account
    # never creates a record nobody can get back to. Checked here rather than in
    # the dependency because it is only connect that must not proceed without
    # one: reading a record you already hold a session for is unaffected.
    if get_settings().require_user_login and (
        principal is None or not principal.is_authenticated
    ):
        raise APIError(
            401,
            ErrorCode.AUTHENTICATION_REQUIRED,
            "Sign in before connecting a provider",
        )

    url, state, ttl = await _begin_authorization(
        session,
        provider=body.provider,
        iss=body.iss,
        link_patient_id=principal.patient_id if principal else None,
        user_id=principal.session.user_id if principal else None,
    )
    return ConnectResponse(authorization_url=url, state=state, expires_in=ttl)


# http://127.0.0.1:8000/auth/start?provider=EPIC_SANDBOX&iss=https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4
@router.get(
    "/auth/start",
    deprecated=True,
    summary="Start the flow by redirecting the browser (deprecated)",
    description="Superseded by `POST /auth/connect`, which returns the "
    "authorization URL instead of redirecting to it and can link a second "
    "provider to an existing patient record. Kept so the current frontend keeps "
    "working; it will be removed once that has moved.",
    # The same refusals as `/auth/connect`, which does the deciding for both —
    # minus the 401, since this route takes no session. Documented rather than
    # left blank because a deprecated route is still one a caller has to handle.
    responses={
        400: refusal(
            "Unknown provider, or an issuer that provider is not registered for",
            ErrorCode.UNKNOWN_PROVIDER,
            ErrorCode.ISSUER_NOT_ALLOWED,
        ),
        429: RATE_LIMITED,
        502: refusal(
            "The server's SMART configuration could not be read",
            ErrorCode.PROVIDER_UNREACHABLE,
        ),
        503: refusal(
            "The provider has no credentials configured in this deployment",
            ErrorCode.PROVIDER_NOT_CONFIGURED,
        ),
    },
)
@limiter.limit(auth_rate_limit)
async def start_auth(
    request: Request,
    provider: str,
    iss: str,
    session: AsyncSession = Depends(get_session),
):
    try:
        url, _state, _ttl = await _begin_authorization(
            session, provider=provider, iss=iss, link_patient_id=None
        )
    except APIError as exc:
        # Returned rather than re-raised because this route redirects on success,
        # so a refusal is the only thing it answers with a body. That body is now
        # the standard envelope: this endpoint predates it and once answered a
        # bare ``{"error": "<sentence>"}``, but one deprecated route keeping a
        # shape of its own is what made this worth a ticket.
        return error_response(exc.status_code, exc.code, exc.message)

    return RedirectResponse(url)


@router.post(
    "/auth/callback",
    response_model=CallbackResponse,
    summary="Exchange the authorization code for a session",
    responses={
        400: refusal(
            "Invalid or expired state, a refused exchange, or an authorization "
            "with no patient context",
            ErrorCode.INVALID_STATE,
            ErrorCode.UNKNOWN_PROVIDER,
            ErrorCode.AUTHORIZATION_REJECTED,
            ErrorCode.NO_PATIENT_CONTEXT,
        ),
        429: RATE_LIMITED,
        502: refusal(
            "The provider could not be reached, or requires an unsupported client "
            "authentication method",
            ErrorCode.PROVIDER_UNREACHABLE,
            ErrorCode.PROVIDER_UNSUPPORTED,
        ),
    },
)
@limiter.limit(auth_rate_limit)
async def handle_callback(
    request: Request,
    callback_data: CallbackData,
    session: AsyncSession = Depends(get_session),
):
    """Complete an authorization and return the session that reads its record.

    Called with the `code` and `state` the provider redirected back with. The
    tokens are stored encrypted and never leave the server; what comes back is a
    session to present on the `/patients/{patientId}/…` routes.
    """
    code = callback_data.code
    state = callback_data.state

    # State now lives in Postgres, so it survives restarts and is shared across workers.
    state_row = await session.get(OAuthState, state)
    if state_row is None or state_row.is_expired:
        raise APIError(400, ErrorCode.INVALID_STATE, "Invalid or expired state")

    # The issuer was validated against the provider's allowlist at /auth/connect
    # and is the only one stored on the state row, so it is trusted here. The
    # same goes for the record to link into: it was checked against a live
    # session when the flow started, so a completed authorization cannot be
    # steered into someone else's record now.
    iss = state_row.iss
    provider = state_row.provider
    code_verifier = state_row.code_verifier
    link_patient_id = state_row.link_patient_id
    user_id = state_row.user_id
    ehr = config.EHR_CONFIGS.get(provider)
    if ehr is None:
        raise APIError(
            400, ErrorCode.UNKNOWN_PROVIDER, "Unknown or unsupported provider"
        )

    # Consume the state so the same authorization cannot be replayed within its TTL.
    await session.delete(state_row)
    await session.commit()

    provider_adapter = provider_for(ehr, iss)
    try:
        smart_config = await provider_adapter.discover(iss)
        token_set = await provider_adapter.exchange_token(
            smart_config, code, code_verifier=code_verifier
        )
    except SMARTDiscoveryError as exc:
        _log_upstream_failure(exc, stage="discovery", provider=provider, iss=iss)
        raise APIError(
            502,
            ErrorCode.PROVIDER_UNREACHABLE,
            "Could not read the server's SMART configuration",
        ) from exc
    except TokenExchangeError as exc:
        # The provider rejected the exchange — a bad code, expired grant, etc.
        _log_upstream_failure(exc, stage="token_exchange", provider=provider, iss=iss)
        raise APIError(
            400, ErrorCode.AUTHORIZATION_REJECTED, "Token exchange failed"
        ) from exc
    except SMARTProviderError as exc:
        # The server requires a client authentication method we do not support.
        _log_upstream_failure(exc, stage="token_exchange", provider=provider, iss=iss)
        raise APIError(
            502, ErrorCode.PROVIDER_UNSUPPORTED, "Unsupported provider configuration"
        ) from exc
    except httpx.HTTPError as exc:
        # Timeout or network failure reaching the provider — an upstream problem.
        # The same wording as the refusal above, under a different status — and
        # now under a different code too, which is what finally tells the two
        # apart for a caller: one is the provider saying no, the other is never
        # having reached it.
        _log_upstream_failure(exc, stage="token_exchange", provider=provider, iss=iss)
        raise APIError(
            502, ErrorCode.PROVIDER_UNREACHABLE, "Token exchange failed"
        ) from exc

    # Where an account started this, the record to join is whatever it owns
    # *now* rather than what it owned when the flow began. The two differ when a
    # patient starts connecting two providers before finishing either: both
    # flows begin with the account holding no record, and taking the state row's
    # answer would have the second authorization anchor a second record, which
    # the account does not point at and nothing can reach.
    #
    # Safe to re-read because it is the account that was checked at /auth/connect
    # and the account that is being read here; what changed in between can only
    # be this same account's own first connection.
    user = await session.get(User, user_id) if user_id else None
    if user is not None:
        link_patient_id = user.patient_id

    # A connection is scoped to a patient, so a token with no patient context
    # cannot anchor one. Refuse rather than store it under an empty id, which two
    # such authorizations would then share.
    if not token_set.patient:
        raise APIError(
            400,
            ErrorCode.NO_PATIENT_CONTEXT,
            "Authorization returned no patient context",
        )

    _log_scope_narrowing(provider, token_set.scope)

    # Passing the link through means the connection joins the record the caller
    # already held. Passing None leaves an existing connection on the record it
    # is already on, so re-authorizing one provider does not strand the others.
    token_row = await persist_token(
        session,
        patient_id=link_patient_id,
        provider=provider,
        iss=iss,
        token_set=token_set,
    )

    # Hand the record to the account that authorized it, where one did and it
    # does not have one yet. This is the whole point of signing in: from here the
    # record belongs to a person rather than to a session, so it is still theirs
    # once this session has expired and they come back.
    #
    # Only when the account holds none. An account that already had one supplied
    # it as link_patient_id above, so the connection joined it and there is
    # nothing to set; assigning here regardless would be how an account came to
    # point at its second record and lose sight of its first.
    if user is not None and user.patient_id is None:
        user.patient_id = token_row.patient_id

    # Issue a session against the record, not the connection, so the bearer
    # reaches everything that record holds and nothing outside it.
    ttl = get_settings().app_session_ttl_seconds
    app_session = AppSession.issue(
        patient_id=token_row.patient_id,
        patient_fhir_id=token_set.patient,  # guaranteed present by the guard above
        provider=provider,
        iss=iss,
        ttl_seconds=ttl,
        user_id=user.id if user is not None else None,
    )
    # Fold the sweeps into the commit that persists this session, so the tables
    # stay tidy without adding a write to the resource read path. Authorizing is
    # also the event that creates the rows being swept, so the cleanup runs
    # exactly as often as the growth does, and needs no scheduler to do it.
    #
    # The new session is written before the sweep rather than after, so the
    # record this request just authorized is reachable by the time anything asks.
    # Otherwise reconnecting a provider left alone for months — the ordinary
    # reason to authorize one again — would meet a sweep still seeing every mark
    # of abandonment on it, because what redeems it had not been written yet, and
    # the request would answer with a session for a record it had just deleted.
    session.add(app_session)
    await delete_expired_sessions(session)
    await session.flush()
    retired = await delete_dead_connections(session)
    await session.commit()
    if retired:
        # Counts only. How fast the table turns over is worth seeing; whose
        # connections they were is not, and this is a log.
        logger.info("Retired %d connection(s) nothing could reach", retired)

    return CallbackResponse(
        patient_id=token_row.patient_id,
        session_id=app_session.session_id,
        expires_in=ttl,
        connection=Connection(
            provider=provider, iss=iss, patient_fhir_id=token_set.patient
        ),
    )
