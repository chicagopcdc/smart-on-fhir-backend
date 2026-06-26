from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse

from pydantic import BaseModel

import secrets
import httpx

from app.providers import config
from app.fhir import service

from contextlib import asynccontextmanager
from functools import lru_cache
from typing import List
from fastapi import  Query
import csv, io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import engine, get_session, delete_expired_states, persist_token
from app.auth.models import OAuthState, ProviderToken
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CallbackData(BaseModel):
    code: str
    state: str


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
async def start_auth(
    provider: str, iss: str, session: AsyncSession = Depends(get_session)
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
    allowed_issuers = {a.rstrip("/") for a in ehr.get("allowed_issuers", [])}
    if iss not in allowed_issuers:
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
async def handle_callback(
    callback_data: CallbackData, session: AsyncSession = Depends(get_session)
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

    await persist_token(session, provider=provider, iss=iss, token_set=token_set)

    return JSONResponse(
        content={"success": True, "patient": token_set.patient}, status_code=200
    )


@app.get("/fhir_resources")
async def get_all_resource(
    fhir_patient_id: str,
    session: AsyncSession = Depends(get_session),
):
    if not fhir_patient_id:
        return JSONResponse({"error": "Missing or unsupported parameters"}, status_code=400)

    # The patient's stored token row knows which provider/issuer they belong to.
    # Most recently updated connection wins if the patient has several.
    token_row = (
        await session.execute(
            select(ProviderToken)
            .where(ProviderToken.patient_fhir_id == fhir_patient_id)
            .order_by(ProviderToken.updated_at.desc())
        )
    ).scalars().first()
    if token_row is None:
        return JSONResponse({"error": "No connected provider for patient"}, status_code=404)

    # The issuer is the FHIR base URL, so it is also the base for resource calls.
    # The access token is read from storage (decrypted by the ORM) rather than
    # taken from the URL, so a bearer token never travels as a query parameter.
    resources = await service.fetch_fhir_resources(
        token_row.access_token, token_row.iss, fhir_patient_id
    )
    return JSONResponse(resources)


# URL to fetch the daily LANTERN CSV data
LANTERN_CSV_URL = "https://lantern.healthit.gov/api/daily/download"

# Maximum allowed number of rows per page in the response
PAGE_SIZE_MAX = 1000

# Cache the result of the function to avoid re-fetching the data on every request
@lru_cache(maxsize=1)
def load_dataset() -> List[dict]:
    # Create a synchronous HTTP client with a 30-second timeout
    with httpx.Client(timeout=30.0) as client:
        # Send a GET request to download the CSV data
        r = client.get(LANTERN_CSV_URL)
        # Raise an exception if the request failed (non-2xx status)
        r.raise_for_status()
    
    # Parse the CSV content into a list of dictionaries
    reader = csv.DictReader(io.StringIO(r.text))
    # Return only rows that have a non-empty "url" field
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
