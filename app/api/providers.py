"""Finding a server to connect to: what this backend is configured for, and the
national endpoint list it can be pointed at."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.providers import config, lantern

router = APIRouter(tags=["providers"])

# Maximum allowed number of rows per page in the response
PAGE_SIZE_MAX = 1000


@router.get("/providers")
async def providers():
    # The providers this backend is configured for (Epic sandbox, and more once they
    # are registered), for the frontend to pin above the Lantern list. These are not in
    # Lantern (it only lists certified production endpoints), so they can only come from
    # our own config. Each carries the provider key /auth/start expects.
    return JSONResponse(config.configured_providers())


# Define an HTTP GET endpoint at path /lantern-endpoints
@router.get("/lantern-endpoints")
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
    # Serve from the in-memory dataset, refreshing it opportunistically. This never
    # raises: on any upstream failure the last-good (or seeded fallback) data is kept,
    # so a bad data source degrades to stale data instead of a 500 that would also
    # strip CORS headers off the response.
    data, source, data_date = await lantern.current_endpoints()

    # Only reachable if the mirror is unavailable AND no provider is configured, since
    # the fallback seeds the dataset from the configured providers. Return a normal
    # response (not an uncaught error) so CORS headers still attach.
    if not data:
        return JSONResponse(
            {"error": "Provider list is temporarily unavailable"}, status_code=503
        )

    # If a query is provided, filter the dataset by matching URL or name (case-insensitive)
    if query:
        q = query.lower()
        data = [
            row for row in data
            if q in row["url"].lower() or q in row["name"].lower()
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
            "name": r["name"],  # Name of the API source
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
            "source": source,  # "mirror" (live file) or "fallback"
            "dataDate": data_date,  # ISO date of the served file, or null for the fallback
        }
    )
