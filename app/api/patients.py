"""Reading a patient's record through the tokens stored for them.

No ``from __future__ import annotations`` here, for the reason spelled out in
``app/api/auth.py``: the rate limiter's wrapper carries slowapi's globals, so
FastAPI cannot resolve a stringified annotation back to the type it names.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    RATE_LIMITED,
    auth_rate_limit,
    fhir_rate_limit,
    get_current_session,
    limiter,
)
from app.api.schemas import (
    ConnectionHealth,
    ConnectionResources,
    Demographics,
    DisconnectResponse,
    ResourceEnvelope,
    ResourcesResponse,
    SummaryIssue,
    SummaryResponse,
    SummarySection,
    refusal,
)
from app.auth import tokens
from app.auth.models import AppSession, ProviderToken, utcnow
from app.core.db import (
    connections_for_patient,
    delete_connections,
    get_session,
    mark_record_used,
)
from app.core.crypto import TokenEncryptionError
from app.core.logging import fields
from app.fhir import normalize, service, summary
from app.providers import config, registry, scopes
from app.providers.discovery import SMARTDiscoveryError
from app.providers.generic import SMARTProviderError

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

    Reaching the record is what keeps it, so this is where that is recorded —
    across every connection, before ``provider`` narrows the result, since
    reading one provider does not make the rest of the record abandoned. The
    filter itself is applied after existence is settled, so one matching nothing
    comes back empty rather than as a missing record.
    """
    if patient_id != app_session.patient_id:
        raise HTTPException(status_code=404, detail="No such patient record")

    connections = await connections_for_patient(session, patient_id)
    if not connections:
        raise HTTPException(status_code=404, detail="No such patient record")
    await mark_record_used(session, connections)

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
# Not needs_reauthorization, and that is the whole point. A token that will not
# decrypt is almost always a key rotation that dropped a key still in use: the
# stored tokens are intact and want the old key back. Sending the patient round
# the consent screen would overwrite the ciphertext under the new key, losing the
# evidence and clearing the symptom while the misconfiguration stands.
UNDECRYPTABLE = _Refusal("This connection cannot be read; the server needs attention")
# A grant this narrow is the one case where consenting again is genuinely the fix.
UNGRANTED = _Refusal(
    "This connection was not granted access to any of the requested records", True
)


def _withheld_by_scope(entry: dict) -> dict:
    """The envelope for a resource type the grant does not cover.

    The shape a failed read already produces, so a consumer needs no new branch:
    this is a type that could not be read, and the reason happens to be one no
    request to the provider would change. ``statusCode`` is null because nothing
    was asked — a 403 here would claim the provider refused something it was never
    sent.
    """
    return normalize.failed_response(
        fhir_type=config.fhir_type_for(entry),
        error="This connection was not granted access to this resource type",
        status_code=None,
    )


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
    except TokenEncryptionError:
        # Reading the row succeeded; reading what is in it did not. Ours to fix,
        # not the patient's, so it is logged as a fault on this side and the rest
        # of the record is left alone.
        logger.error(
            "Stored token for %s could not be decrypted with any configured key",
            token.provider,
            **fields(
                event="connection.token.undecryptable",
                provider=token.provider,
                patient_id=token.patient_id,
            ),
        )
        return _ConnectionRead.refused(token, UNDECRYPTABLE)

    # What this connection was actually granted, which may be less than was asked
    # for. Reading a type the grant excludes buys a 403 and nothing else, so it is
    # reported as withheld instead of spent. Taken from the token just renewed
    # rather than from the row: a refresh may have narrowed the grant, and the
    # instance this request is holding still shows what it was before.
    readable, withheld = scopes.partition(resource_types, live.scope)
    if withheld and not readable:
        return _ConnectionRead.refused(
            token,
            UNGRANTED,
            {name: _withheld_by_scope(entry) for name, entry in withheld.items()},
        )

    read = await service.fetch_fhir_resources(
        live.access_token, token.iss, token.patient_fhir_id, readable
    )
    # Merged back in the order the caller asked for, so a withheld type keeps its
    # place among the rest rather than collecting at the end.
    resources = {
        name: read[name] if name in read else _withheld_by_scope(entry)
        for name, entry in resource_types.items()
    }

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


@router.delete(
    "/patients/{patientId}/connections/{provider}",
    response_model=DisconnectResponse,
    summary="Disconnect a provider from a patient record",
    responses={
        401: UNAUTHORIZED,
        404: refusal("No such record, or that provider is not connected to it"),
        429: RATE_LIMITED,
    },
)
@limiter.limit(auth_rate_limit)
async def disconnect_provider(
    request: Request,
    patient_id: str = PATIENT_ID,
    provider: str = Path(
        description="The connection to end, as it appears on the record.",
        examples=["EPIC_SANDBOX"],
    ),
    app_session: AppSession = Depends(get_current_session),
    session: AsyncSession = Depends(get_session),
):
    """End one provider's connection to this record, and revoke it at the EHR.

    Cleanup otherwise only happens to a caller: connections are retired once
    nothing can reach them, which is a slow answer to "stop reading my chart".
    This is the deliberate one.

    Where the server publishes a revocation endpoint the stored token is
    invalidated there too, so access ends at the EHR rather than only in this
    database. Most servers publish none, and one that does may be down, so the
    connection is removed regardless — a provider having a bad day must not be
    able to keep a patient connected to it.

    Removing a record's last connection removes the record, and the session that
    held it stops resolving.
    """
    connections = await _authorized_connections(
        patient_id, app_session, session, provider
    )
    if not connections:
        raise HTTPException(status_code=404, detail="No such connection on this record")

    # A record can hold one provider more than once — a connection is unique on
    # the patient the server named as well as on the server — and the request
    # named the provider, so all of it goes.
    revoked = [await _revoke_at_provider(connection) for connection in connections]
    remaining = await delete_connections(session, connections)

    return DisconnectResponse(
        provider=provider,
        # Only true where every one of them ended at the EHR as well as here.
        # Claiming otherwise would tell a patient their access was withdrawn
        # somewhere it is still standing.
        revoked_at_provider=all(revoked),
        connections_remaining=remaining,
    )


async def _revoke_at_provider(connection: ProviderToken) -> bool:
    """Ask the EHR to invalidate this connection's token, and never insist.

    The refresh token where there is one, since revoking it takes the access
    tokens issued under the same grant with it; the access token otherwise.

    Every failure here is swallowed on purpose. The caller asked to disconnect,
    not to find out whether the provider was reachable, and letting an outage
    refuse the request would leave them connected to a server they are trying to
    leave. What is lost is only the remote half — our copy goes either way.

    A token that will not decrypt is swallowed the same way, and matters more:
    it is the one a caller is most likely to be here to remove, so the request
    that ends it must not be the request that fails on reading it.
    """
    try:
        token, hint = (
            (connection.refresh_token, "refresh_token")
            if connection.refresh_token is not None
            else (connection.access_token, "access_token")
        )
        adapter = registry.for_connection(connection.provider, connection.iss)
        config = await adapter.discover(connection.iss)
        return await adapter.revoke_token(config, token, token_type_hint=hint)
    except (
        registry.ProviderNotConfigured,
        SMARTDiscoveryError,
        SMARTProviderError,
        TokenEncryptionError,
        httpx.HTTPError,
    ):
        return False


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
    await mark_record_used(session, connections)

    try:
        live = await tokens.live_token(token_row)
    except tokens.TokenRefreshError as exc:
        # This shape predates per-connection reporting, so it carries the wording
        # without the flag beside it that says whether reconnecting would help.
        return JSONResponse({"error": _refusal_for(exc).error}, status_code=502)
    except TokenEncryptionError:
        # The same fault the per-connection paths report as UNDECRYPTABLE. This
        # route reads one connection rather than a record, so there is nothing to
        # keep intact beside it — but it still answers rather than raising, since
        # the frontend that has not moved off this route yet would otherwise see a
        # bare 500 for a problem the newer routes describe.
        logger.error(
            "Stored token for %s could not be decrypted with any configured key",
            token_row.provider,
            **fields(
                event="connection.token.undecryptable",
                provider=token_row.provider,
                patient_id=token_row.patient_id,
            ),
        )
        return JSONResponse({"error": UNDECRYPTABLE.error}, status_code=502)

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
