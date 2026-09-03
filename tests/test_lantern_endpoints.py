"""`/lantern-endpoints` serves the national FHIR endpoint list resiliently.

These flow tests drive the endpoint end to end with the mirror mocked (respx): the
happy path resolves and serves the newest available file, a stale (months-old) file is
served without complaint, and an unavailable source degrades to the curated fallback with
a clean 200 (and CORS headers intact) rather than the uncaught 500 that caused this ticket.
A couple of focused tests pin the newest-file resolution, single-flight caching, and the
drop of rows without a url.
"""

from __future__ import annotations

import asyncio
import csv
import io

import httpx
import pytest
import respx

from app import main
from app.providers import lantern
from tests import upstream

REPO = "onc-healthit/onc-open-data"
BASE = "lantern-daily-data"
CONTENTS = f"https://api.github.com/repos/{REPO}/contents"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"

# The columns the published file actually carries, in the order it carries them.
CSV = (
    '"url","api_information_source_name","certified_api_developer_name",'
    '"capability_fhir_version","smart_http_response"\n'
    '"https://a.example/fhir","Alpha Clinic","Epic Systems Corporation","4.0.1","200"\n'
    '"https://b.example/fhir","Beta Health","Oracle Health","4.0.1","404"\n'
    '"","Gamma (no url, dropped)","X","4.0.1","200"\n'
)


def _dir(*entries: tuple[str, str]) -> list[dict]:
    """A GitHub contents listing of (name, type) entries."""
    return [{"name": name, "type": typ} for name, typ in entries]


def _mock_mirror(year="2025", month="November", filename="11_14_2025endpointdata.csv", csv_text=CSV):
    """Register a mirror that resolves year -> month -> newest file -> CSV, and return
    the top-level listing route so a test can assert how many times it was fetched."""
    years = respx.get(f"{CONTENTS}/{BASE}?ref=main").mock(
        return_value=httpx.Response(200, json=_dir((year, "dir"), (".gitattributes", "file")))
    )
    respx.get(f"{CONTENTS}/{BASE}/{year}?ref=main").mock(
        return_value=httpx.Response(200, json=_dir((month, "dir")))
    )
    respx.get(f"{CONTENTS}/{BASE}/{year}/{month}?ref=main").mock(
        return_value=httpx.Response(200, json=_dir((filename, "file")))
    )
    respx.get(f"{RAW}/{BASE}/{year}/{month}/{filename}").mock(
        return_value=httpx.Response(200, text=csv_text)
    )
    return years


@pytest.fixture(autouse=True)
def _reset_lantern_state():
    lantern.reset_state()
    yield
    lantern.reset_state()


async def _get(headers=None, **params):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        return await client.get("/lantern-endpoints", params=params or None, headers=headers)


@respx.mock
async def test_serves_the_newest_mirror_file_even_when_it_is_old():
    # The only file is months old (the current real-world state of the mirror). It must
    # still be served with a 200, not raise "nothing in the last N days".
    _mock_mirror()

    r = await _get(page=1, pageSize=10)

    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "mirror"
    assert body["dataDate"] == "2025-11-14"
    assert body["totalRows"] == 2  # the no-url Gamma row is dropped
    assert body["rows"][0] == {"idx": 0, "url": "https://a.example/fhir", "name": "Alpha Clinic"}


@respx.mock
async def test_query_filter_and_pagination_still_work():
    _mock_mirror()

    r = await _get(query="alpha")

    body = r.json()
    assert body["totalRows"] == 1
    assert body["rows"][0]["url"] == "https://a.example/fhir"


@respx.mock
async def test_resolver_picks_the_newest_year_month_and_file():
    respx.get(f"{CONTENTS}/{BASE}?ref=main").mock(
        return_value=httpx.Response(200, json=_dir(("2024", "dir"), ("2025", "dir")))
    )
    respx.get(f"{CONTENTS}/{BASE}/2025?ref=main").mock(
        return_value=httpx.Response(200, json=_dir(("September", "dir"), ("November", "dir")))
    )
    respx.get(f"{CONTENTS}/{BASE}/2025/November?ref=main").mock(
        return_value=httpx.Response(
            200, json=_dir(("11_10_2025endpointdata.csv", "file"), ("11_14_2025endpointdata.csv", "file"))
        )
    )
    respx.get(f"{RAW}/{BASE}/2025/November/11_14_2025endpointdata.csv").mock(
        return_value=httpx.Response(200, text=CSV)
    )

    r = await _get()

    assert r.status_code == 200
    assert r.json()["dataDate"] == "2025-11-14"  # newest year, newest month, newest file


@respx.mock(assert_all_called=False)
async def test_degrades_to_curated_fallback_when_source_is_unavailable():
    # The mirror listing fails outright. The endpoint must serve the curated fallback
    # (derived from EHR_CONFIGS) with a 200, never a 500.
    respx.get(f"{CONTENTS}/{BASE}?ref=main").mock(return_value=httpx.Response(500))

    r = await _get()

    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "fallback"
    assert body["totalRows"] >= 1
    assert any("fhir.epic.com" in row["url"] for row in body["rows"])


@respx.mock(assert_all_called=False)
async def test_a_failing_source_still_returns_cors_headers():
    # The exact regression behind this ticket: an uncaught error is produced above the
    # CORS middleware and loses Access-Control-Allow-Origin, so the browser sees an opaque
    # failure. A degraded-but-returned response keeps the header.
    respx.get(f"{CONTENTS}/{BASE}?ref=main").mock(return_value=httpx.Response(500))

    r = await _get(headers={"Origin": "http://localhost:3000"})

    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") is not None


@respx.mock
async def test_refresh_is_single_flight_and_cached_within_ttl():
    years = _mock_mirror()

    await _get()
    await _get()

    # The second request is served from the in-memory dataset (fresh within the TTL), so
    # the upstream is resolved exactly once rather than on every request.
    assert years.call_count == 1


@respx.mock
async def test_a_concurrent_cold_start_burst_triggers_one_refresh():
    # A cold-start burst (what the frontend's retry loop produces) must collapse to a
    # single download via the lock, not a herd. All five requests miss the fresh check
    # and contend on the lock; only the first refreshes.
    years = _mock_mirror()

    responses = await asyncio.gather(*(_get() for _ in range(5)))

    assert years.call_count == 1
    assert all(r.json()["source"] == "mirror" for r in responses)


# --- opt-in: the real mirror. Run with `pytest -m live`. ----------------------
# The source this file mocks is the one that has actually broken, twice: the REST
# endpoint began returning 404 and the mirror that replaced it was bulk-pruned.
# Neither was caught by a test, because a mocked mirror always resolves.

MIRROR = "the Lantern mirror"


@pytest.mark.live
def test_live_the_mirror_still_resolves_to_a_file_with_the_columns_we_read():
    """Resolution and the column names, against the published file.

    Driven through the module's own resolver and its own column constants rather
    than a copy of either, so this cannot pass while the code it guards is
    looking somewhere else.

    It has to reach past `current_endpoints()` to do it. That swallows every
    exception on purpose, so a broken source degrades to the curated fallback
    instead of 500ing above the CORS middleware, which means the public path can
    report that the list is short but never that the source is gone.
    """
    with upstream.reaching(MIRROR), httpx.Client(
        timeout=30.0, follow_redirects=True
    ) as client:
        try:
            # Three listing calls: the base, the newest year, the newest month.
            csv_path, file_date = lantern._resolve_latest_csv(client)
        except lantern.LanternSourceError as exc:
            # The resolver formats the status into its message and chains
            # nothing, so this is the one place the rule is read off text. A
            # spent rate limit is a plausible way to run this — unauthenticated
            # GitHub allows sixty requests an hour — and it answers 403, which
            # is weather here even though a refusal is news elsewhere.
            status = upstream.status_reported_by(exc)
            if status is not None and (upstream.is_outage(status) or status == 403):
                upstream.outage(MIRROR, f"HTTP {status}")
            raise

        raw_url = lantern._RAW_FILE.format(
            repo=lantern._MIRROR_REPO, ref=lantern._MIRROR_REF, path=csv_path
        )
        # Streamed and cut short: the file runs to tens of megabytes and the
        # header plus a few rows answer everything asked here.
        with client.stream("GET", raw_url) as response:
            upstream.served(MIRROR, response)
            assert response.status_code == 200, csv_path
            # Defaulted: an empty file would otherwise leave here as a
            # bare StopIteration, with none of the columns named.
            head = next(response.iter_text(chunk_size=64 * 1024), "")

    # The date is reported, never asserted on. How current the published file is
    # is ONC's decision; whether we can still read it is ours.
    where = f"{csv_path} (dated {file_date})"

    fieldnames = set(csv.DictReader(io.StringIO(head)).fieldnames or [])
    reads = {
        lantern._SRC_URL,
        lantern._SRC_NAME,
        lantern._SRC_VENDOR,
        lantern._SRC_FHIR_VERSION,
        lantern._SRC_SMART_RESPONSE,
    }
    assert reads <= fieldnames, (
        f"{where} no longer carries {sorted(reads - fieldnames)}. Four of those "
        "five are read with .get(), so the parser would keep succeeding and "
        "serve rows with the field silently empty."
    )

    # The real bytes through the real parser, to the last complete row in the
    # chunk. A header that matches while the rows underneath it do not is a shape
    # the column check above cannot see.
    complete_rows = head[: head.rfind("\n") + 1]
    parsed = lantern._parse_csv(complete_rows)
    assert any(row["vendor"] for row in parsed), f"{where} carries no vendor names"
    assert any(row["smartCapable"] for row in parsed), (
        f"{where} reports no SMART-capable endpoint in its first rows"
    )
