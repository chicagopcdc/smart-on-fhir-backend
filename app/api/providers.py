"""Finding a server to connect to: what this backend is configured for, the
national endpoint list it can be pointed at, and whether a given endpoint is
usable right now.

Deliberately no ``from __future__ import annotations`` here, for the reason
``app/api/auth.py`` sets out at length: the rate limiter wraps an endpoint with
``functools.wraps``, which cannot carry a function's ``__globals__`` across, so
FastAPI would resolve a stringified annotation in slowapi's namespace instead of
this one and quietly misread the parameter. Real annotation objects sidestep it.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import RATE_LIMITED, endpoint_check_rate_limit, limiter
from app.api.schemas import (
    ConfiguredProvider,
    EndpointCheckResponse,
    EndpointCheckStatus,
    ProviderSearchResponse,
    ProviderSearchRow,
    refusal,
)
from app.providers import config, discovery, lantern, registry
from app.providers.discovery import DiscoveryResult, SMARTDiscoveryError
from app.providers.targets import UnresolvedTarget, UnsafeTarget, ensure_fetchable

router = APIRouter(tags=["providers"])

# Maximum allowed number of rows per page in the response
PAGE_SIZE_MAX = 1000

UNAVAILABLE = "Provider list is temporarily unavailable"

# Fixed here rather than taken from the exception, whose message carries the URL
# fetched and the parser's complaint — neither is text this API repeats back.
_CHECK_DETAIL: dict[EndpointCheckStatus, str] = {
    "ok": "The endpoint published a SMART configuration.",
    "unreachable": "The endpoint could not be reached just now. Some servers refuse "
    "an unauthenticated request for their configuration, so this is worth a warning "
    "rather than treating the endpoint as unusable.",
    "no_smart_configuration": "The endpoint is reachable but publishes no SMART "
    "configuration, so it cannot be used to sign in.",
    "invalid_smart_configuration": "The endpoint publishes a SMART configuration "
    "that cannot be used to sign in.",
}


def _configured_by_issuer() -> dict[str, str]:
    """Issuer -> provider key, for the endpoints this backend can authorize.

    Keyed on the exact issuer and nothing looser. Two hospitals running the same
    EHR are separate tenants with separate registrations, so a vendor match says
    nothing about whether we hold credentials for a given endpoint.
    """
    return {p["iss"]: p["provider"] for p in config.configured_providers()}


@router.get(
    "/providers/search",
    response_model=ProviderSearchResponse,
    summary="Search the national list of FHIR endpoints",
    responses={503: refusal("No endpoint data is available to serve")},
)
async def search_providers(
    query: str = Query(
        "",
        description="Free-text match on the endpoint URL and the organization "
        "exposing it, case-insensitive.",
        examples=["children"],
    ),
    vendor: str = Query(
        "",
        description="Match on the certified EHR developer. This is what "
        "separates endpoints *served by* a vendor from organizations that merely "
        "have its name in theirs.",
        examples=["Epic"],
    ),
    smart_only: bool = Query(
        False,
        alias="smartOnly",
        description="Keep only endpoints that served a SMART configuration when "
        "ONC last probed them, dropping the ones that failed and the ones it "
        "could not reach.",
    ),
    page: int = Query(1, ge=1, description="1-based page index"),
    page_size: int = Query(
        50, ge=1, le=PAGE_SIZE_MAX, alias="pageSize", description="Rows per page"
    ),
):
    """Search ONC's daily list of certified FHIR endpoints.

    The URL of a row is the `iss` to pass to `POST /auth/connect`. Rows carry
    `configured` and `provider` where this backend holds a registration that can
    actually authorize against them.

    Served from an in-memory copy of the published file, refreshed daily. If the
    source is unreachable the last good data is served rather than an error, so a
    bad day upstream degrades to stale results instead of an empty dropdown.
    """
    data, source, data_date = await lantern.current_endpoints()
    if not data:
        # Only reachable if the source is unavailable and no provider is
        # configured, since the fallback seeds the dataset from the configured
        # providers. Raised, not returned: what costs a response its CORS headers
        # is going unhandled, and an HTTPException is handled inside the
        # middleware stack, so this keeps them and the API's one refusal shape.
        raise HTTPException(status_code=503, detail=UNAVAILABLE)

    matches = lantern.search(data, query=query, vendor=vendor, smart_only=smart_only)
    configured = _configured_by_issuer()

    start, end = (page - 1) * page_size, page * page_size
    rows = [
        ProviderSearchRow(
            idx=idx,
            url=row["url"],
            name=row["name"],
            vendor=row.get("vendor"),
            fhir_version=row.get("fhirVersion"),
            smart_capable=row.get("smartCapable"),
            smart_capable_as_of=data_date,
            configured=row["url"].rstrip("/") in configured,
            provider=configured.get(row["url"].rstrip("/")),
        )
        for idx, row in enumerate(matches[start:end], start=start)
    ]

    return ProviderSearchResponse(
        page=page,
        page_size=page_size,
        total_rows=len(matches),
        has_more=end < len(matches),
        source=source,
        data_date=data_date,
        rows=rows,
    )


@router.get(
    "/providers/endpoint-check",
    response_model=EndpointCheckResponse,
    summary="Check whether an endpoint can be used, now",
    responses={
        400: refusal("The issuer is not one this backend will fetch"),
        429: RATE_LIMITED,
    },
)
@limiter.limit(endpoint_check_rate_limit)
async def check_endpoint(
    request: Request,
    iss: str = Query(
        description="The FHIR base URL to check, as it would be passed to "
        "`POST /auth/connect`.",
        examples=["https://launch.smarthealthit.org/v/r4/fhir"],
    ),
):
    """Ask an endpoint, now, whether it can be used to sign a patient in.

    The `smartCapable` flag on a search row is ONC's, recorded whenever it last
    probed, and the published file can be months old. This reads the endpoint's
    own `.well-known/smart-configuration` and answers for the present, so a dead
    or non-SMART endpoint is ruled out before a user is sent to a login screen
    that will never appear.

    A negative is an answer, not an error: only an issuer this backend refuses to
    fetch is a `400`.

    What it does not say is whether we could *authorize* against the endpoint — a
    separate and stricter fact, which the `configured` flag on a search row answers.
    """
    try:
        checked = await ensure_fetchable(iss)
    except UnsafeTarget as exc:
        # Raised rather than returned: what costs a response its CORS headers is
        # going unhandled, and an HTTPException is handled inside the middleware
        # stack. See the 503 in search_providers above.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnresolvedTarget:
        # A name that no longer resolves is a fact about the endpoint, not a bad
        # request, so it answers like any other endpoint that cannot be reached.
        return _checked(iss.strip().rstrip("/"), "unreachable")

    try:
        result = await registry.discovery.fetch_result(checked)
    except SMARTDiscoveryError as exc:
        return _checked(checked, discovery.failure_status(exc))

    return _checked(checked, "ok", result)


def _checked(
    iss: str, status: EndpointCheckStatus, result: DiscoveryResult | None = None
) -> EndpointCheckResponse:
    """One check's answer, with the fields that depend on each other kept in step.

    ``checkedAt`` is the fetch's own timestamp when there is one; a failure was
    never cached, so those are happening now.
    """
    discovered = result.configuration if result else None
    return EndpointCheckResponse(
        iss=iss,
        status=status,
        reachable=status != "unreachable",
        smart_capable=status == "ok",
        authorization_endpoint=(
            str(discovered.authorization_endpoint) if discovered else None
        ),
        token_endpoint=str(discovered.token_endpoint) if discovered else None,
        detail=_CHECK_DETAIL[status],
        checked_at=(
            result.fetched_at if result else datetime.now(timezone.utc)
        ).isoformat(timespec="seconds"),
    )


@router.get(
    "/providers",
    response_model=list[ConfiguredProvider],
    summary="The providers this backend is configured for",
)
async def providers():
    """The providers this backend holds credentials for, each with the key
    `POST /auth/connect` expects.

    These are not in the national list, which covers certified production
    endpoints only, so a sandbox can be offered from nowhere else.
    """
    return config.configured_providers()


@router.get(
    "/lantern-endpoints",
    deprecated=True,
    summary="Search the endpoint list (deprecated)",
    description="Superseded by `GET /providers/search`, which adds vendor and "
    "SMART-capability filtering and says which endpoints this backend can "
    "authorize against. Kept, with its original response shape, so the current "
    "frontend keeps working; it will be removed once that has moved.",
)
async def lantern_endpoints(
    query: str = Query("", description="Free-text search, case-insensitive"),
    page: int = Query(1, ge=1, description="1-based page index"),
    page_size: int = Query(
        500, ge=1, le=PAGE_SIZE_MAX, alias="pageSize", description="Rows per page"
    ),
):
    data, source, data_date = await lantern.current_endpoints()
    if not data:
        return JSONResponse({"error": UNAVAILABLE}, status_code=503)

    matches = lantern.search(data, query=query)
    start, end = (page - 1) * page_size, page * page_size

    return JSONResponse(
        {
            "page": page,
            "pageSize": page_size,
            "totalRows": len(matches),
            "hasMore": end < len(matches),
            "rows": [
                {"idx": idx, "url": r["url"], "name": r["name"]}
                for idx, r in enumerate(matches[start:end], start=start)
            ],
            "source": source,  # "mirror" (live file) or "fallback"
            "dataDate": data_date,  # ISO date of the served file, or null for the fallback
        }
    )
