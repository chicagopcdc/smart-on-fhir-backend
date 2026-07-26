"""The national FHIR endpoint list (ONC Lantern), served resiliently.

This list powers the frontend's "pick a provider" dropdown. Its upstream has proven
unstable twice: ONC's REST download API began returning 404, and the GitHub mirror we
moved to (PR #12) was later bulk-pruned so its newest file is months old. So this module
never depends on a live fetch to answer a request. It serves an **in-memory dataset** that
is seeded at import from a small curated fallback (the providers this backend is actually
configured for) and refreshed opportunistically from the mirror's newest available file.

Any upstream failure leaves the last good data in place, so ``/lantern-endpoints`` returns
data (200) rather than an uncaught 500 — which matters because a 500 is produced above the
CORS middleware and would strip ``Access-Control-Allow-Origin``, turning a backend hiccup
into an opaque browser failure that the frontend then retries in a loop.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import sys
import time
from datetime import date

import httpx
from starlette.concurrency import run_in_threadpool

from app.providers import config

logger = logging.getLogger(__name__)

# The mirror source. These are constants, not settings: if ONC moves or restructures
# the repo the file naming changes too, so recovering means a code change regardless
# (as this PR itself is). Keeping them here, in one place, is the right level of knob.
_MIRROR_REPO = "onc-healthit/onc-open-data"
_MIRROR_PATH = "lantern-daily-data"
_MIRROR_REF = "main"
_GITHUB_CONTENTS = "https://api.github.com/repos/{repo}/contents/{path}"
_RAW_FILE = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}

# Serve a fetched list for a day before refreshing; after a failed refresh, wait
# before retrying so a request burst cannot hammer a down upstream; per-request
# network timeout for resolving and downloading the file.
_REFRESH_TTL_SECONDS = 24 * 60 * 60
_RETRY_AFTER_FAILURE_SECONDS = 10 * 60
_FETCH_TIMEOUT_SECONDS = 30.0

# English month directory name -> number, so month segments sort chronologically
# (a plain string sort would put "April" before "January").
_MONTHS = {
    name: num
    for num, name in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"],
        start=1,
    )
}

# A daily file is named like ``07_08_2026endpointdata.csv``.
_FILE_RE = re.compile(r"^(\d{2})_(\d{2})_(\d{4})endpointdata\.csv$")

# The CSV columns we read; the served rows keep only these, under neutral keys, so
# the endpoint never has to know the source's column names.
#
# The vendor is the certified EHR developer, which is a different thing from the
# organization exposing the endpoint and is what makes a useful search: matching
# "epic" against organization names finds a few dozen businesses that happen to be
# named Epic, while matching it against the developer finds the several hundred
# endpoints Epic actually serves.
#
# ONC fetches each endpoint's `.well-known/smart-configuration` when it builds the
# file, so `smart_http_response` is a daily reachability check we would otherwise
# have to run ourselves, and the FHIR version says whether our R4 reads apply at
# all before anyone is sent through a login.
_SRC_URL = "url"
_SRC_NAME = "api_information_source_name"
_SRC_VENDOR = "certified_api_developer_name"
_SRC_FHIR_VERSION = "capability_fhir_version"
_SRC_SMART_RESPONSE = "smart_http_response"


class LanternSourceError(RuntimeError):
    """The upstream endpoint list could not be resolved, fetched, or parsed."""


# --- in-memory dataset + refresh state --------------------------------------
# All refreshes go through ``_lock`` (single-flight), so a burst of requests — e.g.
# the frontend retrying — triggers at most one download rather than a herd. Elapsed
# time uses monotonic clocks so a system clock change cannot skew the TTL or backoff.

_dataset: list[dict] = []              # rows of {"url", "name"}
_data_date: str | None = None          # ISO date of the served file; None => the fallback
_loaded_monotonic: float | None = None    # last *successful* refresh
_failed_monotonic: float | None = None    # last *failed* refresh (cleared on success)
_lock = asyncio.Lock()


def _curated_fallback() -> list[dict]:
    """The endpoint-list fallback served when the mirror is unreachable: the configured
    providers projected to the row shape the search returns, so the dropdown and the
    connect flow keep working even if the mirror is deleted.

    ``smartCapable`` is True because these are the servers this backend already
    authorizes against, so their SMART configuration is known good. The vendor and
    FHIR version are unknown rather than guessed: they come from ONC's certification
    data, and a sandbox is not in it."""
    return [
        {
            "url": p["iss"],
            "name": p["name"],
            "vendor": None,
            "fhirVersion": None,
            "smartCapable": True,
        }
        for p in config.configured_providers()
    ]


# Seed at import so there is always something to serve before any network call.
_dataset = _curated_fallback()


# --- source resolution + fetch (blocking; run off the event loop) -----------


def _list_dir(client: httpx.Client, path: str) -> list[dict]:
    """Return the GitHub contents listing for a directory, or raise."""
    r = client.get(
        _GITHUB_CONTENTS.format(repo=_MIRROR_REPO, path=path),
        params={"ref": _MIRROR_REF},
        headers=_GITHUB_HEADERS,
    )
    if r.status_code != 200:
        raise LanternSourceError(f"listing {path!r} returned {r.status_code}")
    body = r.json()
    if not isinstance(body, list):
        raise LanternSourceError(f"listing {path!r} was not a directory")
    return body


def _newest_csv_in_month(client: httpx.Client, year: str, month: str) -> tuple[str, date] | None:
    """The newest ``endpointdata.csv`` in one month directory, or None if empty."""
    best: tuple[str, date] | None = None
    for entry in _list_dir(client, f"{_MIRROR_PATH}/{year}/{month}"):
        m = _FILE_RE.match(entry.get("name", ""))
        if not m:
            continue
        mm, dd, yyyy = (int(g) for g in m.groups())
        try:
            d = date(yyyy, mm, dd)
        except ValueError:
            continue
        if best is None or d > best[1]:
            best = (f"{_MIRROR_PATH}/{year}/{month}/{entry['name']}", d)
    return best


def _resolve_latest_csv(client: httpx.Client) -> tuple[str, date]:
    """Find the newest daily CSV by walking newest year -> month -> file.

    Makes no assumption that a *recent* file exists, so a pruned or frozen mirror
    still resolves to its latest available data. Years and months are tried newest
    first, and the search skips an empty newest directory rather than giving up, so a
    stray empty month cannot mask an older-but-present file.
    """
    years = sorted(
        (e["name"] for e in _list_dir(client, _MIRROR_PATH)
         if e.get("type") == "dir" and e["name"].isdigit()),
        key=int,
        reverse=True,
    )
    for year in years:  # newest first; returns on the first year that has a file
        months = sorted(
            (e["name"] for e in _list_dir(client, f"{_MIRROR_PATH}/{year}")
             if e.get("type") == "dir" and e["name"] in _MONTHS),
            key=lambda name: _MONTHS[name],
            reverse=True,
        )
        for month in months:
            found = _newest_csv_in_month(client, year, month)
            if found:
                return found
    raise LanternSourceError("no endpoint CSV found in the mirror")


def _smart_capable(value: str | None) -> bool | None:
    """Whether the endpoint served a SMART configuration when ONC last probed it.

    The column holds the HTTP status that probe received. A 0 means the request
    never completed, which says the endpoint was unreachable rather than that it
    is not SMART, so it reads as unknown rather than as a no.
    """
    if not value or not value.strip().isdigit():
        return None
    status = int(value)
    return True if status == 200 else (None if status == 0 else False)


def _parse_csv(text: str) -> list[dict]:
    """Parse the CSV to the rows we serve, dropping any without a url.

    An empty result means the columns changed (or we fetched an error page), so it
    is treated as a source failure rather than served as a real list.

    The file runs to tens of thousands of rows and only a few hundred distinct
    vendors, so vendor strings are interned: without it the wider row would cost a
    separate copy of the same name for every endpoint that developer certified.
    """
    reader = csv.DictReader(io.StringIO(text))
    rows = [
        {
            "url": row[_SRC_URL],
            "name": row.get(_SRC_NAME, ""),
            "vendor": sys.intern(row.get(_SRC_VENDOR) or "") or None,
            "fhirVersion": sys.intern(row.get(_SRC_FHIR_VERSION) or "") or None,
            "smartCapable": _smart_capable(row.get(_SRC_SMART_RESPONSE)),
        }
        for row in reader
        if row.get(_SRC_URL)
    ]
    if not rows:
        raise LanternSourceError("parsed 0 usable rows (unexpected CSV shape?)")
    return rows


def search(
    rows: list[dict],
    *,
    query: str = "",
    vendor: str = "",
    smart_only: bool = False,
) -> list[dict]:
    """Narrow the endpoint list, matching case-insensitively on substrings.

    ``query`` searches the URL and the organization; ``vendor`` searches the
    certified developer, which is the filter that separates "served by Epic" from
    "has Epic in its name". ``smart_only`` keeps the endpoints ONC's last probe
    found serving a SMART configuration, dropping both the outright failures and
    the ones it could not reach.

    One pass rather than one per filter: the dataset runs to tens of thousands of
    rows, so a filter per criterion would walk and copy all of them again each
    time. An unfiltered search returns the dataset itself and copies nothing,
    which is the common case behind a dropdown's first page.
    """
    if not (query or vendor or smart_only):
        return rows

    q, v = query.lower(), vendor.lower()
    return [
        row
        for row in rows
        if (not q or q in row["url"].lower() or q in row["name"].lower())
        and (not v or v in (row.get("vendor") or "").lower())
        and (not smart_only or row.get("smartCapable"))
    ]


def _fetch_latest_blocking() -> tuple[list[dict], str]:
    """Resolve + download + parse the newest mirror file. Blocking, so callers run it
    in a worker thread. Returns (rows, iso_date)."""
    with httpx.Client(timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
        csv_path, file_date = _resolve_latest_csv(client)
        r = client.get(_RAW_FILE.format(repo=_MIRROR_REPO, ref=_MIRROR_REF, path=csv_path))
        r.raise_for_status()
        # utf-8-sig transparently drops a byte-order mark if the file has one, so the
        # first CSV column header stays "url" rather than a BOM-prefixed key.
        text = r.content.decode("utf-8-sig")
    return _parse_csv(text), file_date.isoformat()


# --- refresh orchestration + public surface ---------------------------------


def _current_source() -> str:
    return "mirror" if _data_date else "fallback"


def _is_fresh() -> bool:
    return _loaded_monotonic is not None and (time.monotonic() - _loaded_monotonic) < _REFRESH_TTL_SECONDS


def _in_failure_backoff() -> bool:
    """True if the last refresh failed recently, so a request storm cannot re-hammer a
    down upstream on every request."""
    return _failed_monotonic is not None and (time.monotonic() - _failed_monotonic) < _RETRY_AFTER_FAILURE_SECONDS


async def _refresh() -> None:
    global _dataset, _data_date, _loaded_monotonic, _failed_monotonic
    try:
        rows, iso_date = await run_in_threadpool(_fetch_latest_blocking)
    except Exception as exc:  # noqa: BLE001 — any failure must degrade, never propagate
        _failed_monotonic = time.monotonic()
        logger.warning("Lantern refresh failed; serving %s data: %s", _current_source(), exc)
        return
    _dataset, _data_date, _loaded_monotonic, _failed_monotonic = rows, iso_date, time.monotonic(), None
    logger.info("Lantern refreshed: %d rows from mirror file %s", len(rows), iso_date)


async def current_endpoints() -> tuple[list[dict], str, str | None]:
    """Return ``(rows, source, data_date)``, refreshing the in-memory dataset first if
    it is stale. Never raises: on any upstream failure the current (last-good or fallback)
    data is kept and served. ``rows`` are ``{"url", "name"}``; ``source`` is "mirror" or
    "fallback"; ``data_date`` is the served file's ISO date, or None for the fallback.
    """
    if not _is_fresh():
        # Single-flight: concurrent callers wait for the one in-flight refresh instead
        # of each starting their own download, then serve its result.
        async with _lock:
            if not (_is_fresh() or _in_failure_backoff()):
                await _refresh()
    return _dataset, _current_source(), _data_date


def reset_state() -> None:
    """Restore the import-time state (seeded fallback, no timestamps). For tests."""
    global _dataset, _data_date, _loaded_monotonic, _failed_monotonic
    _dataset = _curated_fallback()
    _data_date = None
    _loaded_monotonic = None
    _failed_monotonic = None
