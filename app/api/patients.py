"""Reading a patient's record through the tokens stored for them.

No ``from __future__ import annotations`` here, for the reason spelled out in
``app/api/auth.py``: the rate limiter's wrapper carries slowapi's globals, so
FastAPI cannot resolve a stringified annotation back to the type it names.
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import fhir_rate_limit, get_current_session, limiter
from app.auth.models import AppSession, ProviderToken
from app.core.db import get_session
from app.fhir import service
from app.providers import config

router = APIRouter(tags=["patients"])


@router.get("/fhir_resources")
@limiter.limit(fhir_rate_limit)
async def get_all_resource(
    request: Request,
    include: config.ResourceTier = Query(
        config.ResourceTier.US_CORE,
        description="Which slice of the record to read: the US Core set a certified "
        "server must support, or every configured resource type",
    ),
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
        token_row.access_token, token_row.iss, app_session.patient_fhir_id, tier=include
    )

    # Keyed by fetch config row, so the two Observation searches stay separate.
    # The request context (tier, patient, provider) travels alongside so a caller
    # need not remember what it asked for to read a partial record.
    return JSONResponse(
        {
            "include": include.value,
            "patient": app_session.patient_fhir_id,
            "provider": app_session.provider,
            "iss": app_session.iss,
            "resources": resources,
        }
    )
