"""Finding a server to connect to: what this backend is configured for, and the
national endpoint list it can be pointed at."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.api.schemas import (
    ConfiguredProvider,
    ProviderSearchResponse,
    ProviderSearchRow,
)
from app.providers import config, lantern

router = APIRouter(tags=["providers"])

# Maximum allowed number of rows per page in the response
PAGE_SIZE_MAX = 1000

UNAVAILABLE = "Provider list is temporarily unavailable"


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
    responses={503: {"description": "No endpoint data is available to serve"}},
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
        # providers. A normal response rather than an uncaught error, so CORS
        # headers still attach.
        return JSONResponse({"detail": UNAVAILABLE}, status_code=503)

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
