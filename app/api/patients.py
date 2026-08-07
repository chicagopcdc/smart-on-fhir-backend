"""Reading a patient's record through the tokens stored for them.

No ``from __future__ import annotations`` here, for the reason spelled out in
``app/api/auth.py``: the rate limiter's wrapper carries slowapi's globals, so
FastAPI cannot resolve a stringified annotation back to the type it names.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RATE_LIMITED, fhir_rate_limit, get_current_session, limiter
from app.api.schemas import (
    ConnectionHealth,
    ConnectionResources,
    Demographics,
    ResourceEnvelope,
    ResourcesResponse,
    SummaryIssue,
    SummaryResponse,
    SummarySection,
    refusal,
)
from app.auth import tokens
from app.auth.models import AppSession, ProviderToken, utcnow
from app.core.db import connections_for_patient, get_session
from app.fhir import service, summary
from app.providers import config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["patients"])

# Aliased so the documented path reads {patientId}, matching the camelCase the rest
# of the API uses, while the parameter keeps its Python spelling.
PATIENT_ID = Path(
    alias="patientId",
    description="The patient record to read, as returned by `POST /auth/callback`.",
    examples=["pat_9Fq3TnVb2mKd7sXwLp4RcYhZ"],
)

NOT_FOUND = refusal("No such record, or the session does not hold it")
UNAUTHORIZED = refusal("Missing, invalid, or expired session")


async def _authorized_connections(
    patient_id: str,
    app_session: AppSession,
    session: AsyncSession,
    provider: str | None = None,
) -> list[ProviderToken]:
    """The connections under ``patient_id``, once the session is entitled to it.

    A record the session does not hold is a 404 rather than a 403: confirming an
    id exists is itself a leak. A record with no connections left answers the
    same, since a record exists only for as long as something hangs off it.

    ``provider`` narrows the result after existence is settled, so a filter
    matching nothing comes back empty rather than as a missing record.
    """
    if patient_id != app_session.patient_id:
        raise HTTPException(status_code=404, detail="No such patient record")

    connections = await connections_for_patient(session, patient_id)
    if not connections:
        raise HTTPException(status_code=404, detail="No such patient record")

    if provider is not None:
        connections = [token for token in connections if token.provider == provider]
    return connections


@dataclass(frozen=True)
class _Refusal:
    """Why a connection came back empty, and whether reconnecting would help.

    The two travel together because they are one judgement about one outcome.
    Pairing them by hand at each place a read can fail is how they would come to
    disagree — a wording telling the patient to reconnect beside a flag saying
    nothing is wrong.
    """

    error: str
    needs_reauthorization: bool = False


# The first two are the provider's answer about the authorization itself; the
# rest are our own reading of what came back.
REVOKED = _Refusal("This connection is no longer authorized; reconnect this provider", True)
UNRENEWABLE = _Refusal(
    "This provider could not be reached to renew access; try again shortly"
)
EXPIRED = _Refusal("The stored token has expired; reconnect this provider", True)
UNREADABLE = _Refusal("No resource could be read from this provider")
UNMODELLED = _Refusal("This provider could not be read")


def _refusal_for(error: tokens.TokenRefreshError) -> _Refusal:
    """How a failure to renew a token reads to whoever asked for the record."""
    return REVOKED if isinstance(error, tokens.ReauthorizationRequired) else UNRENEWABLE


@dataclass(frozen=True)
class _ConnectionRead:
    """One connection's read, and how completely it came back.

    Carries the connection alongside its outcome so the two never have to be
    paired back up by position afterwards.
    """

    token: ProviderToken
    resources: dict
    status: str
    error: str | None
    needs_reauthorization: bool = False

    @classmethod
    def refused(
        cls, token: ProviderToken, refusal: _Refusal, resources: dict | None = None
    ) -> "_ConnectionRead":
        """A connection that answered nothing, and the one reason it did not."""
        return cls(
            token,
            resources or {},
            "error",
            refusal.error,
            needs_reauthorization=refusal.needs_reauthorization,
        )

    @property
    def reported(self) -> dict:
        """The fields every connection reports, whichever response wraps it."""
        return {
            "provider": self.token.provider,
            "iss": self.token.iss,
            "patient_fhir_id": self.token.patient_fhir_id,
            "status": self.status,
            "error": self.error,
            "needs_reauthorization": self.needs_reauthorization,
        }

    @property
    def envelopes(self) -> dict[str, ResourceEnvelope]:
        return {
            name: ResourceEnvelope.model_validate(envelope)
            for name, envelope in self.resources.items()
        }


async def _read_connection(token: ProviderToken, resource_types: dict) -> _ConnectionRead:
    """Read one connection, and say how completely it could be read.

    The issuer is the FHIR base URL, so it is also the base for resource calls.
    The access token comes from storage (decrypted by the ORM) rather than from
    the URL, so a bearer token never travels as a query parameter — and it is
    renewed here first where it would otherwise lapse partway through the fan-out
    below, which is why the token used is the one ``live_token`` hands back and
    not the one still on the row.
    """
    try:
        live = await tokens.live_token(token)
    except tokens.TokenRefreshError as exc:
        return _ConnectionRead.refused(token, _refusal_for(exc))

    resources = await service.fetch_fhir_resources(
        live.access_token, token.iss, token.patient_fhir_id, resource_types
    )

    failed = [
        name
        for name, envelope in resources.items()
        if envelope.get("status") != "ok"
    ]
    if not failed:
        return _ConnectionRead(token, resources, "ok", None)
    if len(failed) < len(resources):
        return _ConnectionRead(token, resources, "degraded", None)

    # Nothing came back at all. A token that has run out with nothing to renew
    # it from is by far the likeliest reason and the only one the caller can act
    # on, so say so rather than repeating whichever refusal the provider
    # happened to word first.
    expired = live.expires_at is not None and live.expires_at <= utcnow()
    return _ConnectionRead.refused(token, EXPIRED if expired else UNREADABLE, resources)


async def _read_all(
    connections: list[ProviderToken], resource_types: dict
) -> list[_ConnectionRead]:
    """Read every connection at once, keeping each one's outcome separate.

    Concurrent because a record spanning three providers should take as long as
    the slowest, not the sum. ``return_exceptions`` so one provider failing in a
    way the fetch layer does not model still leaves the others readable — the
    whole point of reporting per connection.
    """
    results = await asyncio.gather(
        *(_read_connection(token, resource_types) for token in connections),
        return_exceptions=True,
    )

    reads = []
    for token, result in zip(connections, results):
        if isinstance(result, _ConnectionRead):
            reads.append(result)
            continue
        # The caller is told only that the connection failed, so this is the one
        # record of why. The type and not the message, because what arrives here
        # is whatever the fetch layer did not model — most likely a validation
        # error out of normalization, and pydantic quotes the value of the field
        # that failed, which on a Patient is a name or a date of birth. The
        # provider key and our own record id are ours to name.
        logger.error(
            "Unhandled %s reading %s for %s",
            type(result).__name__,
            token.provider,
            token.patient_id,
        )
        reads.append(_ConnectionRead.refused(token, UNMODELLED))
    return reads


@router.get(
    "/patients/{patientId}/resources",
    response_model=ResourcesResponse,
    summary="Read a patient's normalized resources",
    responses={401: UNAUTHORIZED, 404: NOT_FOUND, 429: RATE_LIMITED},
)
@limiter.limit(fhir_rate_limit)
async def read_resources(
    request: Request,
    patient_id: str = PATIENT_ID,
    resource_type: list[config.ResourceName] = Query(
        default=None,
        alias="type",
        description="Which resource types to read. Repeat for several. When "
        "omitted, `include` decides.",
    ),
    include: config.ResourceTier = Query(
        config.ResourceTier.US_CORE,
        description="Which slice of the record to read when no `type` is given: "
        "the US Core set a certified server must support, or every configured "
        "resource type. `all` reaches types that are not scoped to a patient and "
        "is a diagnostic affordance rather than something to read on a patient's "
        "behalf.",
    ),
    provider: str = Query(
        default=None,
        description="Read only this connection, rather than every provider on "
        "the record.",
        examples=["EPIC_SANDBOX"],
    ),
    app_session: AppSession = Depends(get_current_session),
    session: AsyncSession = Depends(get_session),
):
    """Read the patient's record, as normalized resources, one entry per connection.

    Every response carries the same keys whether a read succeeded or failed, so a
    consumer never has to branch on shape to find out something is missing.

    The record is not merged across providers. The same person's Condition at two
    hospitals is two resources on two servers, and joining them here would assert
    something the data cannot support. `GET /patients/{patientId}/summary` is the
    merged view.
    """
    connections = await _authorized_connections(
        patient_id, app_session, session, provider
    )

    resource_types = (
        config.resources_named(name.value for name in resource_type)
        if resource_type
        else config.resources_for(include)
    )
    reads = await _read_all(connections, resource_types)

    return ResourcesResponse(
        patient_id=patient_id,
        include=include.value,
        types=list(resource_types),
        connections=[
            ConnectionResources(**read.reported, resources=read.envelopes)
            for read in reads
        ],
    )


@router.get(
    "/patients/{patientId}/summary",
    response_model=SummaryResponse,
    summary="Read a patient's clinical summary",
    responses={401: UNAUTHORIZED, 404: NOT_FOUND, 429: RATE_LIMITED},
)
@limiter.limit(fhir_rate_limit)
async def read_summary(
    request: Request,
    patient_id: str = PATIENT_ID,
    limit: int = Query(
        20,
        ge=1,
        le=100,
        description="Most-recent items to return per section. The section's "
        "`total` still reports everything the servers hold.",
    ),
    provider: str = Query(
        default=None,
        description="Summarize only this connection.",
        examples=["EPIC_SANDBOX"],
    ),
    app_session: AppSession = Depends(get_current_session),
    session: AsyncSession = Depends(get_session),
):
    """Summarize the patient's chart across every provider they have connected.

    Merged, unlike the resource read: a summary is a view rather than a record,
    so gathering conditions from two hospitals into one list is the point. Each
    item keeps the connection it came from.

    A provider that is down does not sink the summary. Its connection is reported
    as failed, what could not be read is listed under `issues`, and the rest of
    the chart comes back as a normal 200.
    """
    connections = await _authorized_connections(
        patient_id, app_session, session, provider
    )

    reads = await _read_all(connections, config.resources_named(summary.SUMMARY_RESOURCES))
    per_connection = [(read.token.provider, read.resources) for read in reads]

    demographics = summary.build_demographics(per_connection)
    return SummaryResponse(
        patient_id=patient_id,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        demographics=Demographics(**demographics) if demographics else None,
        connections=[ConnectionHealth(**read.reported) for read in reads],
        sections=[
            SummarySection.model_validate(section)
            for section in summary.build_sections(per_connection, limit=limit)
        ],
        issues=[
            SummaryIssue.model_validate(issue)
            for issue in summary.collect_issues(per_connection)
        ],
    )


@router.get(
    "/fhir_resources",
    deprecated=True,
    summary="Read the session's connection (deprecated)",
    description="Superseded by `GET /patients/{patientId}/resources`, which names "
    "the record being read and covers every provider on it. Kept so the current "
    "frontend keeps working; it will be removed once that has moved.",
)
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
    # This endpoint predates patient records, so it reads the one connection the
    # session was issued for rather than everything the record holds. Matched on
    # the full identity, so it stays the same connection even once the record has
    # several.
    connections = await connections_for_patient(session, app_session.patient_id)
    token_row = next(
        (
            token
            for token in connections
            if token.provider == app_session.provider
            and token.iss == app_session.iss
            and token.patient_fhir_id == app_session.patient_fhir_id
        ),
        None,
    )
    if token_row is None:
        return JSONResponse({"error": "No connected provider for patient"}, status_code=404)

    try:
        live = await tokens.live_token(token_row)
    except tokens.TokenRefreshError as exc:
        # This shape predates per-connection reporting, so it carries the wording
        # without the flag beside it that says whether reconnecting would help.
        return JSONResponse({"error": _refusal_for(exc).error}, status_code=502)

    resources = await service.fetch_fhir_resources(
        live.access_token,
        token_row.iss,
        app_session.patient_fhir_id,
        config.resources_for(include),
    )

    return JSONResponse(
        {
            "include": include.value,
            "patient": app_session.patient_fhir_id,
            "provider": app_session.provider,
            "iss": app_session.iss,
            "resources": resources,
        }
    )
