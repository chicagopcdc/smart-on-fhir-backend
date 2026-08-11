"""`/providers/search`: finding an endpoint you can actually connect to.

The problem this endpoint exists for is that searching the national list by name
is close to useless. "epic" matches every organization with Epic in its name and
none of the several hundred endpoints Epic serves, so a user picks something that
cannot complete a login. The published file already carries the certified
developer and the result of ONC's own daily SMART probe; these tests drive the
filtering built on them, and check that the endpoint the frontend still calls is
unchanged.

The mirror is mocked (respx); everything downstream of it is the real application.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app import main
from app.providers import config, lantern

REPO = "onc-healthit/onc-open-data"
BASE = "lantern-daily-data"
CONTENTS = f"https://api.github.com/repos/{REPO}/contents"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
FILE = "11_14_2025endpointdata.csv"

# A small stand-in for the real file, shaped like it: an organization named after
# a vendor it does not run, endpoints that failed ONC's SMART probe, and one that
# could not be reached at all.
CSV = (
    '"url","api_information_source_name","certified_api_developer_name",'
    '"capability_fhir_version","smart_http_response"\n'
    '"https://one.example/fhir","Riverbend Health","Epic Systems Corporation","4.0.1","200"\n'
    '"https://two.example/fhir","Lakeside Clinic","Epic Systems Corporation","4.0.1","404"\n'
    '"https://three.example/fhir","Epicurean Wellness","Practice Fusion","4.0.1","200"\n'
    '"https://four.example/fhir","Northgate Medical","Oracle Health","1.0.2","200"\n'
    '"https://five.example/fhir","Summit Care","Oracle Health","4.0.1","0"\n'
)


def _dir(*entries: tuple[str, str]) -> list[dict]:
    return [{"name": name, "type": typ} for name, typ in entries]


def _mock_mirror(csv_text: str = CSV) -> None:
    respx.get(f"{CONTENTS}/{BASE}?ref=main").mock(
        return_value=httpx.Response(200, json=_dir(("2025", "dir")))
    )
    respx.get(f"{CONTENTS}/{BASE}/2025?ref=main").mock(
        return_value=httpx.Response(200, json=_dir(("November", "dir")))
    )
    respx.get(f"{CONTENTS}/{BASE}/2025/November?ref=main").mock(
        return_value=httpx.Response(200, json=_dir((FILE, "file")))
    )
    respx.get(f"{RAW}/{BASE}/2025/November/{FILE}").mock(
        return_value=httpx.Response(200, text=csv_text)
    )


@pytest.fixture(autouse=True)
def _reset_lantern_state():
    lantern.reset_state()
    yield
    lantern.reset_state()


async def _search(path="/providers/search", **params):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        return await client.get(path, params=params or None)


def _urls(body: dict) -> list[str]:
    return [row["url"] for row in body["rows"]]


@respx.mock
async def test_a_name_search_finds_the_organization_not_the_vendor():
    _mock_mirror()

    body = (await _search(query="epic")).json()

    # The problem, reproduced: the only name-match is a wellness business with no
    # relationship to the EHR the user meant.
    assert _urls(body) == ["https://three.example/fhir"]


@respx.mock
async def test_a_vendor_search_finds_the_endpoints_that_vendor_serves():
    _mock_mirror()

    body = (await _search(vendor="epic")).json()

    # The two endpoints Epic actually serves, and not the business named after it.
    assert _urls(body) == ["https://one.example/fhir", "https://two.example/fhir"]
    assert {row["vendor"] for row in body["rows"]} == {"Epic Systems Corporation"}


@respx.mock
async def test_smart_only_drops_what_the_probe_could_not_confirm():
    _mock_mirror()

    body = (await _search(smartOnly=True)).json()

    # Kept: the ones that answered 200. Dropped: the 404 (not SMART) and the 0
    # (ONC could not reach it at all, which is unknown rather than a no).
    assert _urls(body) == [
        "https://one.example/fhir",
        "https://three.example/fhir",
        "https://four.example/fhir",
    ]


@respx.mock
async def test_the_filters_combine():
    _mock_mirror()

    body = (await _search(vendor="epic", smartOnly=True)).json()

    assert _urls(body) == ["https://one.example/fhir"]
    assert body["totalRows"] == 1


@respx.mock
async def test_a_row_carries_what_a_caller_needs_to_decide():
    _mock_mirror()

    body = (await _search(query="riverbend")).json()

    assert body["rows"] == [
        {
            "idx": 0,
            "url": "https://one.example/fhir",
            "name": "Riverbend Health",
            "vendor": "Epic Systems Corporation",
            "fhirVersion": "4.0.1",
            "smartCapable": True,
            # Dated per row, so a row rendered on its own still says how old its
            # certification flag is. `GET /providers/endpoint-check` is the live one.
            "smartCapableAsOf": "2025-11-14",
            "configured": False,
            "provider": None,
        }
    ]


@respx.mock
async def test_an_endpoint_this_backend_can_authorize_says_so():
    """The bridge from search to connect: a row that carries a provider key can
    be passed straight to /auth/connect."""
    configured = config.configured_providers()[0]
    _mock_mirror(
        '"url","api_information_source_name","certified_api_developer_name",'
        '"capability_fhir_version","smart_http_response"\n'
        f'"{configured["iss"]}","Sandbox","Epic Systems Corporation","4.0.1","200"\n'
    )

    [row] = (await _search()).json()["rows"]

    assert row["configured"] is True
    assert row["provider"] == configured["provider"]


@respx.mock
async def test_a_shared_vendor_never_implies_a_registration():
    """Two hospitals on one EHR are separate tenants with separate logins, so
    matching the vendor of a configured provider must not mark an endpoint as
    connectable."""
    _mock_mirror()

    body = (await _search(vendor="epic")).json()

    assert all(row["configured"] is False for row in body["rows"])
    assert all(row["provider"] is None for row in body["rows"])


@respx.mock
async def test_paging_reports_the_whole_result_set():
    _mock_mirror()

    first = (await _search(page=1, pageSize=2)).json()
    second = (await _search(page=2, pageSize=2)).json()
    last = (await _search(page=3, pageSize=2)).json()

    assert first["totalRows"] == second["totalRows"] == 5
    assert first["hasMore"] is True and last["hasMore"] is False
    # idx is a position in the full result set, so a caller can page without
    # losing track of where a row sits.
    assert [row["idx"] for row in second["rows"]] == [2, 3]


@respx.mock(assert_all_called=False)
async def test_an_unavailable_source_still_offers_what_we_can_connect_to():
    respx.get(f"{CONTENTS}/{BASE}?ref=main").mock(return_value=httpx.Response(500))

    body = (await _search()).json()

    assert body["source"] == "fallback"
    assert body["dataDate"] is None
    # These are the servers this backend already authorizes against, so they are
    # known SMART-capable; their vendor is not in ONC's data and is not guessed.
    assert all(row["configured"] and row["smartCapable"] for row in body["rows"])
    assert all(row["vendor"] is None for row in body["rows"])
    # Undated, because that flag is this backend's own claim rather than a reading
    # from a published file, and dating it would pass one off as the other.
    assert all(row["smartCapableAsOf"] is None for row in body["rows"])


@respx.mock
async def test_the_endpoint_the_frontend_calls_is_unchanged():
    """The deprecated route keeps the exact shape its caller reads."""
    _mock_mirror()

    body = (await _search("/lantern-endpoints", query="riverbend")).json()

    assert body["rows"] == [
        {"idx": 0, "url": "https://one.example/fhir", "name": "Riverbend Health"}
    ]
    assert set(body) == {
        "page", "pageSize", "totalRows", "hasMore", "rows", "source", "dataDate"
    }
