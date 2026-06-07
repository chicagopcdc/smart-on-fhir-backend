from fastapi import FastAPI, HTTPException, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.datastructures import URL

from pydantic import BaseModel

import secrets
import httpx
import base64

import config
import service

from contextlib import asynccontextmanager
from datetime import timedelta
from functools import lru_cache
from typing import List, Optional
from fastapi import  Query
import csv, io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import engine, get_session, delete_expired_states
from models import OAuthState, ProviderToken, utcnow
from providers.models import TokenSet
from settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Nothing to warm up on startup; on shutdown, release the connection pool.
    yield
    await engine.dispose()


app = FastAPI(lifespan=lifespan)

origins = ["http://localhost:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# app.add_middleware(
#     CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
# )

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
    ttl_seconds = get_settings().oauth_state_ttl_seconds
    session.add(
        OAuthState(
            state=state,
            iss=iss,
            provider=provider,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )
    )
    await session.commit()

    # scope and aud can be omitted
    auth_url = (
        f"{iss}{ehr['authorize_url']}?"
        f"response_type=code&"
        f"client_id={ehr['client_id']}&"
        f"redirect_uri={ehr['redirect_uri']}&"
        f"scope={ehr['scopes']}&"
        f"state={state}&"
        f"aud=https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
    )
    # return JSONResponse(content={"auth_url": auth_url})
    return RedirectResponse(auth_url)

# f"aud={iss + ehr['fhir_server_url']}"


async def persist_token(
    session: AsyncSession, *, provider: str, iss: str, token_set: TokenSet
) -> ProviderToken:
    """Insert or update the stored token for a patient/provider/issuer.

    The (patient_fhir_id, provider, iss) triple is unique, so re-authenticating
    updates the existing row instead of leaving a stale token behind.
    """
    patient_fhir_id = token_set.patient or ""
    existing = await session.execute(
        select(ProviderToken).where(
            ProviderToken.patient_fhir_id == patient_fhir_id,
            ProviderToken.provider == provider,
            ProviderToken.iss == iss,
        )
    )
    token = existing.scalar_one_or_none()
    if token is None:
        token = ProviderToken(
            patient_fhir_id=patient_fhir_id, provider=provider, iss=iss
        )
        session.add(token)

    token.access_token = token_set.access_token
    token.refresh_token = token_set.refresh_token
    token.scope = token_set.scope
    token.expires_at = (
        utcnow() + timedelta(seconds=token_set.expires_in)
        if token_set.expires_in is not None
        else None
    )
    await session.commit()
    return token


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

    credentials = f"{ehr['client_id']}:{ehr['client_secret']}".encode("utf-8")
    basic_auth_header = base64.b64encode(credentials).decode("utf-8")

    headers = {
        "Authorization": f"Basic {basic_auth_header}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ehr["redirect_uri"]
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(iss + ehr["token_url"], data=data, headers=headers)
        token_response = response.json()

    if response.status_code != 200 or "access_token" not in token_response:
        raise HTTPException(status_code=400, detail="Token exchange failed")

    # TokenSet drops vendor-specific keys (e.g. __epic.dstu2.patient) before storage.
    token_set = TokenSet.model_validate(token_response)
    await persist_token(session, provider=provider, iss=iss, token_set=token_set)

    return JSONResponse(content={"success": True}, status_code=200)


@app.get("/fhir_resources")
async def get_all_resource(
    access_token: str,
    fhir_patient_id: str,
    state: str,
    session: AsyncSession = Depends(get_session),
):
    if not access_token or not fhir_patient_id or not state:
        return JSONResponse({"error": "Missing or unsupported parameters"}, status_code=400)

    state_row = await session.get(OAuthState, state)
    if state_row is None:
        return JSONResponse({"error": "Invalid or expired state"}, status_code=400)

    ehr = config.EHR_CONFIGS.get(state_row.provider)
    resources = await service.fetch_fhir_resources(
        access_token, state_row.iss + ehr["fhir_server_url"], fhir_patient_id
    )
    return JSONResponse(resources)




# LANTERN_URL = "https://lantern.healthit.gov/api/endpoint-manager/endpoints"
# # https://lantern.healthit.gov/api/daily/download
# @app.get("/fhir-endpoints")
# async def get_fhir_endpoints():
#     async with httpx.AsyncClient() as client:
#         res = await client.get(LANTERN_URL)
#         res.raise_for_status()
#         data = res.json()

#     # Filter and transform
#     options = [
#         {
#             "label": f"{item.get('organizationName', 'Unknown')} ({item.get('url')})",
#             "value": item["url"]
#         }
#         for item in data
#         if item.get("url") and item.get("organizationName")
#     ]

#     return options





# LANTERN_CSV_URL = "https://lantern.healthit.gov/api/daily/download"

# @app.get("/lantern-csv")
# async def get_lantern_csv():
#     try:
#         async with httpx.AsyncClient() as client:
#             response = await client.get(LANTERN_CSV_URL)
#             response.raise_for_status()
#         return Response(content=response.text, media_type="text/csv")
#     except httpx.HTTPError as e:
#         return Response(content=f"Error fetching CSV: {str(e)}", status_code=500)



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
