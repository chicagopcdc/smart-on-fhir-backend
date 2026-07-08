"""The Lantern endpoint list: resolving the latest mirror CSV, then serving it.

The upstream ONC download API is gone, so the data comes from Lantern's public
GitHub mirror. These tests mock the mirror (respx) and pin the expected URLs to the
current date, so they stay deterministic whenever they run.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
import respx

from app import main

CSV = (
    '"url","api_information_source_name","api_developer_name"\n'
    '"https://a.example/fhir","Alpha Clinic","Epic Systems Corporation"\n'
    '"https://b.example/fhir","Beta Health","Oracle Health"\n'
    '"","Gamma (no url, dropped)","X"\n'
)


def _url_for(day: date) -> str:
    return f"{main.LANTERN_MIRROR_BASE}/{day:%Y}/{day:%B}/{day:%m_%d_%Y}endpointdata.csv"


@pytest.fixture(autouse=True)
def _clear_dataset_cache():
    main.load_dataset.cache_clear()
    yield
    main.load_dataset.cache_clear()


@respx.mock
def test_resolver_skips_a_missing_day_and_picks_the_newest_that_exists():
    today = date.today()
    yesterday = today - timedelta(days=1)
    respx.head(_url_for(today)).mock(return_value=httpx.Response(404))
    respx.head(_url_for(yesterday)).mock(return_value=httpx.Response(200))

    assert main._latest_lantern_csv_url() == _url_for(yesterday)


@respx.mock
def test_load_dataset_parses_rows_and_drops_rows_without_a_url():
    today = date.today()
    respx.head(_url_for(today)).mock(return_value=httpx.Response(200))
    respx.get(_url_for(today)).mock(return_value=httpx.Response(200, text=CSV))

    data = main.load_dataset()

    assert [row["url"] for row in data] == [
        "https://a.example/fhir",
        "https://b.example/fhir",
    ]


@respx.mock
async def test_lantern_endpoints_serves_paginated_rows():
    today = date.today()
    respx.head(_url_for(today)).mock(return_value=httpx.Response(200))
    respx.get(_url_for(today)).mock(return_value=httpx.Response(200, text=CSV))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.get("/lantern-endpoints", params={"page": 1, "pageSize": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["totalRows"] == 2
    assert body["rows"][0]["url"] == "https://a.example/fhir"
    assert body["rows"][0]["name"] == "Alpha Clinic"
