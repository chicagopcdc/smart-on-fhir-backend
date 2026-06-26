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
from app.providers.models import TokenSet
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


# http://127.0.0.1:8000/auth/start?provider=EPIC_SANDBOX&iss=https://fhir.epic.com/interconnect-fhir-oauth
@app.get("/auth/start")
async def start_auth(
    provider: str, iss: str, session: AsyncSession = Depends(get_session)
):
    ehr = config.EHR_CONFIGS.get(provider)
    if not ehr:
        return JSONResponse({"error": "Unknown or unsupported issuer (iss)"}, status_code=400)

    # Opportunistic sweep so expired anti-CSRF state does not accumulate.
    await delete_expired_states(session)

    state = secrets.token_urlsafe(16)
    session.add(
        OAuthState.issue(state, iss, provider, get_settings().oauth_state_ttl_seconds)
    )
    await session.commit()

    auth_url = (
        f"{iss}{ehr['authorize_url']}?"
        f"response_type=code&"
        f"client_id={ehr['client_id']}&"
        f"redirect_uri={ehr['redirect_uri']}&"
        f"scope={ehr['scopes']}&"
        f"state={state}&"
        f"aud=https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
    )
    return RedirectResponse(auth_url)


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

    iss = state_row.iss
    provider = state_row.provider
    ehr = config.EHR_CONFIGS.get(provider)
    if ehr is None:
        raise HTTPException(status_code=400, detail="Unknown or unsupported provider")

    # Consume the state so the same authorization cannot be replayed within its TTL.
    await session.delete(state_row)
    await session.commit()

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ehr["redirect_uri"],
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                iss + ehr["token_url"],
                data=data,
                auth=(ehr["client_id"], ehr["client_secret"]),
            )
    except httpx.HTTPError:
        # Timeout or network failure reaching the provider — an upstream
        # problem, not a bad request from our caller.
        raise HTTPException(status_code=502, detail="Token exchange failed")

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Token exchange failed")

    try:
        token_response = response.json()
    except ValueError:
        # A 200 with a non-JSON body (e.g. an HTML error page from a gateway).
        raise HTTPException(status_code=502, detail="Token exchange failed")

    if "access_token" not in token_response:
        raise HTTPException(status_code=400, detail="Token exchange failed")

    # TokenSet drops vendor-specific keys (e.g. __epic.dstu2.patient) before storage.
    token_set = TokenSet.model_validate(token_response)
    await persist_token(session, provider=provider, iss=iss, token_set=token_set)

    return JSONResponse(content={"success": True}, status_code=200)


@app.get("/fhir_resources")
async def get_all_resource(
    access_token: str,
    fhir_patient_id: str,
    session: AsyncSession = Depends(get_session),
):
    if not access_token or not fhir_patient_id:
        return JSONResponse({"error": "Missing or unsupported parameters"}, status_code=400)

    # The OAuth state was consumed at the callback, so the patient's stored
    # token row is what knows which provider/issuer this patient belongs to.
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

    ehr = config.EHR_CONFIGS.get(token_row.provider)
    if ehr is None:
        return JSONResponse({"error": "Unknown or unsupported provider"}, status_code=400)

    resources = await service.fetch_fhir_resources(
        access_token, token_row.iss + ehr["fhir_server_url"], fhir_patient_id
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
