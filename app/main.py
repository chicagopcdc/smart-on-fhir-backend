from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from pydantic import BaseModel

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import secrets
import httpx

from app.providers import config
from app.fhir import service

from contextlib import asynccontextmanager
from functools import lru_cache
from typing import List
from fastapi import  Query
import csv, io
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import (
    engine,
    get_session,
    delete_expired_states,
    delete_expired_sessions,
    persist_token,
)
from app.auth.models import AppSession, OAuthState, ProviderToken
from app.providers.discovery import SMARTDiscovery, SMARTDiscoveryError
from app.providers.generic import (
    GenericSMARTProvider,
    SMARTProviderError,
    TokenExchangeError,
)
from app.core.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(lifespan=lifespan)

# Throttle per client IP. The auth endpoints trigger outbound discovery and DB
# writes, and the resource endpoint fans out to many FHIR calls, so both are
# abuse-amplification surfaces worth capping. Limits are read from settings at
# request time so they can be tuned without touching this file.
#
# get_remote_address keys on the socket peer, which is correct for a direct
# connection. Behind a reverse proxy or load balancer that becomes the proxy's
# IP (one shared bucket for everyone), so a proxied deployment must add trusted
# X-Forwarded-For handling (e.g. Starlette's ProxyHeadersMiddleware) and key on
# the forwarded client address.
limiter = Limiter(
    key_func=get_remote_address, enabled=get_settings().rate_limit_enabled
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Only the known frontend may call the API from a browser. Credentials are not
# used (the session travels as a bearer token, not a cookie), so a wildcard
# origin is neither needed nor safe to combine with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().resolved_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

class CallbackData(BaseModel):
    code: str
    state: str


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

    app_session = await session.get(AppSession, credentials.credentials)
    if app_session is None or app_session.is_expired:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return app_session


# A single discovery cache shared across requests: re-authorizing the same
# issuer reuses its SMART configuration instead of re-fetching it each time.
_discovery = SMARTDiscovery()


def _provider_for(ehr: dict, iss: str) -> GenericSMARTProvider:
    """Build the discovery-driven provider for one provider/issuer connection.

    The token is bound to the issuer the app intends to call, so aud is the
    issuer — never a hardcoded server.
    """
    return GenericSMARTProvider(
        client_id=ehr["client_id"],
        client_secret=ehr.get("client_secret"),
        redirect_uri=ehr["redirect_uri"],
        aud=iss.rstrip("/"),
        discovery=_discovery,
    )


# http://127.0.0.1:8000/auth/start?provider=EPIC_SANDBOX&iss=https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4
@app.get("/auth/start")
@limiter.limit(lambda: get_settings().auth_rate_limit)
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

    provider_adapter = _provider_for(ehr, iss)
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


@app.post("/auth/callback")
@limiter.limit(lambda: get_settings().auth_rate_limit)
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

    provider_adapter = _provider_for(ehr, iss)
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


@app.get("/fhir_resources")
@limiter.limit(lambda: get_settings().fhir_rate_limit)
async def get_all_resource(
    request: Request,
    app_session: AppSession = Depends(get_current_session),
    session: AsyncSession = Depends(get_session),
):
    # The session (resolved and validated by get_current_session) pins the exact
    # connection, so look the token up by its full identity rather than by patient
    # id alone. The patient comes from the session, never from the request, so one
    # caller can never read another's record.
    token_row = (
        await session.execute(
            select(ProviderToken).where(
                ProviderToken.patient_fhir_id == app_session.patient_fhir_id,
                ProviderToken.provider == app_session.provider,
                ProviderToken.iss == app_session.iss,
            )
        )
    ).scalars().first()
    if token_row is None:
        return JSONResponse({"error": "No connected provider for patient"}, status_code=404)

    # The issuer is the FHIR base URL, so it is also the base for resource calls.
    # The access token is read from storage (decrypted by the ORM) rather than
    # taken from the URL, so a bearer token never travels as a query parameter.
    resources = await service.fetch_fhir_resources(
        token_row.access_token, token_row.iss, app_session.patient_fhir_id
    )
    return JSONResponse(resources)


# ONC's Lantern REST download API (lantern.healthit.gov/api/daily/download) started
# returning 404 in mid-2026. Lantern publishes the same daily endpoint CSV to a
# public GitHub repo, so we read the most recent file from there. Files are named
# lantern-daily-data/<year>/<Month>/<MM_DD_YYYY>endpointdata.csv.
LANTERN_MIRROR_BASE = (
    "https://raw.githubusercontent.com/onc-healthit/onc-open-data/main/lantern-daily-data"
)

# How many days back to look for the newest published file. The mirror can lag a
# day or two, so we search a window rather than assuming today's file exists.
LANTERN_LOOKBACK_DAYS = 14

# Full month names, indexed 1..12. The mirror path uses English month names, so we
# build the segment explicitly rather than with strftime("%B"), which would follow
# the process locale and break on a non-English one.
_MONTHS = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Maximum allowed number of rows per page in the response
PAGE_SIZE_MAX = 1000


def _latest_lantern_csv_url() -> str:
    """Return the URL of the newest endpoint CSV in the Lantern mirror.

    Walks back from today and returns the first daily file that exists, so a
    missing or not-yet-published day is skipped automatically. The result is
    cached by load_dataset, so this fan-out of HEAD requests runs at most once per
    process on the happy path (today's file exists -> a single HEAD). A short
    per-request timeout bounds the worst case if GitHub is unreachable.
    """
    today = date.today()
    with httpx.Client(timeout=8.0, follow_redirects=True) as client:
        for offset in range(LANTERN_LOOKBACK_DAYS):
            day = today - timedelta(days=offset)
            url = (
                f"{LANTERN_MIRROR_BASE}/{day.year}/{_MONTHS[day.month]}/"
                f"{day:%m_%d_%Y}endpointdata.csv"
            )
            if client.head(url).status_code == 200:
                return url
    raise RuntimeError(
        f"No Lantern endpoint CSV found in the last {LANTERN_LOOKBACK_DAYS} days"
    )


# Cache the result so the ~30 MB file is downloaded and parsed once per process.
@lru_cache(maxsize=1)
def load_dataset() -> List[dict]:
    url = _latest_lantern_csv_url()
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()

    reader = csv.DictReader(io.StringIO(r.text))
    return [row for row in reader if row.get("url")]

# Define an HTTP GET endpoint at path /lantern-endpoints
@app.get("/lantern-endpoints")
async def lantern_endpoints(
    # Query string for searching endpoints, optional (default: empty string)
    query: str = Query("", description="Free-text search, case-insensitive"),
    # Page number for pagination (must be at least 1)
    page: int = Query(1, ge=1, description="1-based page index"),
    # Number of rows per page (between 1 and PAGE_SIZE_MAX), accessed via query param `pageSize`
    page_size: int = Query(
        500, ge=1, le=PAGE_SIZE_MAX, alias="pageSize",
        description="Rows per page",
    ),
):
    # Load the full dataset from cache or fetch if not cached
    data = load_dataset()

    # If a query is provided, filter the dataset by matching URL or name (case-insensitive)
    if query:
        q = query.lower()
        data = [
            row for row in data
            if q in row["url"].lower() or q in row["api_information_source_name"].lower()
        ]

    # Calculate start and end indices for pagination
    start, end = (page - 1) * page_size, page * page_size
    # Get the slice of data for the current page
    slice_ = data[start:end]

    # Build a list of rows with index, URL, and name for the response
    rows = [
        {
            "idx": idx,  # Global index of the row
            "url": r["url"],  # Endpoint URL
            "name": r["api_information_source_name"],  # Name of the API source
        }
        for idx, r in enumerate(slice_, start=start)
    ]

    # Return the paginated result as a JSON response
    return JSONResponse(
        {
            "page": page,  # Current page number
            "pageSize": page_size,  # Page size
            "totalRows": len(data),  # Total number of matching rows
            "hasMore": end < len(data),  # Whether there are more pages
            "rows": rows,  # The data rows for this page
        }
    )
