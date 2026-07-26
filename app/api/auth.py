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

import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import auth_rate_limit, limiter, provider_for
from app.auth.models import AppSession, OAuthState
from app.core.config import get_settings
from app.core.db import (
    delete_expired_sessions,
    delete_expired_states,
    get_session,
    persist_token,
)
from app.providers import config
from app.providers.discovery import SMARTDiscoveryError
from app.providers.generic import SMARTProviderError, TokenExchangeError

router = APIRouter(tags=["authorization"])


class CallbackData(BaseModel):
    code: str
    state: str


# http://127.0.0.1:8000/auth/start?provider=EPIC_SANDBOX&iss=https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4
@router.get("/auth/start")
@limiter.limit(auth_rate_limit)
async def start_auth(
    request: Request,
    provider: str,
    iss: str,
    session: AsyncSession = Depends(get_session),
):
    ehr = config.EHR_CONFIGS.get(provider)
    if not ehr:
        return JSONResponse({"error": "Unknown or unsupported provider"}, status_code=400)

    # A provider with no configured client_id is a deployment that did not set
    # its credentials; fail clearly here rather than redirect the user to the
    # EHR with an empty client_id and let them hit an opaque invalid_client.
    if not ehr.get("client_id"):
        return JSONResponse({"error": "Provider is not configured"}, status_code=503)

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
        return JSONResponse(
            {"error": "Issuer not allowed for this provider"}, status_code=400
        )

    provider_adapter = provider_for(ehr, iss)
    try:
        smart_config = await provider_adapter.discover(iss)
    except SMARTDiscoveryError:
        return JSONResponse(
            {"error": "Could not read the server's SMART configuration"}, status_code=502
        )

    # Opportunistic sweep so expired anti-CSRF state does not accumulate.
    await delete_expired_states(session)

    state = secrets.token_urlsafe(16)
    auth = provider_adapter.build_auth_url(smart_config, state, ehr["scopes"].split())
    session.add(
        OAuthState.issue(
            state,
            iss,
            provider,
            get_settings().oauth_state_ttl_seconds,
            code_verifier=auth.code_verifier,
        )
    )
    await session.commit()

    return RedirectResponse(auth.url)


@router.post("/auth/callback")
@limiter.limit(auth_rate_limit)
async def handle_callback(
    request: Request,
    callback_data: CallbackData,
    session: AsyncSession = Depends(get_session),
):
    code = callback_data.code
    state = callback_data.state

    # State now lives in Postgres, so it survives restarts and is shared across workers.
    state_row = await session.get(OAuthState, state)
    if state_row is None or state_row.is_expired:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    # The issuer was validated against the provider's allowlist at /auth/start
    # and is the only one stored on the state row, so it is trusted here.
    iss = state_row.iss
    provider = state_row.provider
    code_verifier = state_row.code_verifier
    ehr = config.EHR_CONFIGS.get(provider)
    if ehr is None:
        raise HTTPException(status_code=400, detail="Unknown or unsupported provider")

    # Consume the state so the same authorization cannot be replayed within its TTL.
    await session.delete(state_row)
    await session.commit()

    provider_adapter = provider_for(ehr, iss)
    try:
        smart_config = await provider_adapter.discover(iss)
        token_set = await provider_adapter.exchange_token(
            smart_config, code, code_verifier=code_verifier
        )
    except SMARTDiscoveryError:
        raise HTTPException(
            status_code=502, detail="Could not read the server's SMART configuration"
        )
    except TokenExchangeError:
        # The provider rejected the exchange — a bad code, expired grant, etc.
        raise HTTPException(status_code=400, detail="Token exchange failed")
    except SMARTProviderError:
        # The server requires a client authentication method we do not support.
        raise HTTPException(status_code=502, detail="Unsupported provider configuration")
    except httpx.HTTPError:
        # Timeout or network failure reaching the provider — an upstream problem.
        raise HTTPException(status_code=502, detail="Token exchange failed")

    # A session is scoped to a patient, so a token with no patient context cannot
    # anchor one. Refuse rather than store it under an empty id, which two such
    # authorizations would then share.
    if not token_set.patient:
        raise HTTPException(
            status_code=400, detail="Authorization returned no patient context"
        )

    await persist_token(session, provider=provider, iss=iss, token_set=token_set)

    # Issue a session bound to the patient just authorized. The frontend presents
    # it to read resources, so access is scoped to this patient rather than to a
    # patient id the caller could name.
    app_session = AppSession.issue(
        patient_fhir_id=token_set.patient,  # guaranteed present by the guard above
        provider=provider,
        iss=iss,
        ttl_seconds=get_settings().app_session_ttl_seconds,
    )
    # Fold an expired-session sweep into the commit that persists this one, so the
    # table stays tidy without adding a write to the resource read path.
    await delete_expired_sessions(session)
    session.add(app_session)
    await session.commit()

    return JSONResponse(
        content={
            "success": True,
            "patient": token_set.patient,
            "session_id": app_session.session_id,
        },
        status_code=200,
    )
