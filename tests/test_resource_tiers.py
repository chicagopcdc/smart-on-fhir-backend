"""What the backend asks the EHR for, and what it takes to change that.

Reading a record used to mean one request per entry in ``RESOURCE_FETCH_CONFIG``,
every one of them, on every call. These tests pin the smaller default: the US
Core set a certified server must support, with the long tail reachable through
``?include=all``.

They assert on the requests that left the process rather than on the response
body, because "which resources do we fetch" is the behaviour under test and it
should stay pinned however the response is later shaped.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.providers.config import (
    RESOURCE_FETCH_CONFIG,
    US_CORE_RESOURCES,
    fhir_type_for,
    validate_us_core_membership,
)
from tests.app_harness import SMART_LAUNCHER, read_resources

FHIR_PATH = "/v/r4/fhir"

EMPTY_BUNDLE = {"resourceType": "Bundle", "type": "searchset", "entry": []}

# The ten requests a default read should make, written out rather than derived
# from the config so that widening the default tier has to be a deliberate edit
# here too. Each is the path plus the `category` that separates the Observation
# searches from one another.
US_CORE_REQUESTS = {
    (f"{FHIR_PATH}/Patient/erXuFYUfucBZaryVksYEcMg3", None),
    (f"{FHIR_PATH}/AllergyIntolerance", None),
    (f"{FHIR_PATH}/Condition", None),
    (f"{FHIR_PATH}/DiagnosticReport", None),
    (f"{FHIR_PATH}/Encounter", None),
    (f"{FHIR_PATH}/Immunization", None),
    (f"{FHIR_PATH}/MedicationRequest", None),
    (f"{FHIR_PATH}/Observation", "vital-signs"),
    (f"{FHIR_PATH}/Observation", "laboratory"),
    (f"{FHIR_PATH}/Procedure", None),
}


def _requests_made(route) -> set[tuple[str, str | None]]:
    """Each outbound FHIR call as (path, category), the pair that identifies it."""
    made = set()
    for call in route.calls:
        url = urlparse(str(call.request.url))
        category = parse_qs(url.query).get("category", [None])[0]
        made.add((url.path, category))
    return made


async def _read_resources(tmp_path, token_response, params=None):
    """Authorize against the launcher, then read resources with the mocked FHIR server."""
    return await read_resources(
        SMART_LAUNCHER,
        f"sqlite+aiosqlite:///{tmp_path / 'tiers.db'}",
        token_response=token_response,
        responder=lambda request: httpx.Response(200, json=EMPTY_BUNDLE),
        params=params,
    )


@respx.mock
async def test_a_default_read_asks_only_for_the_us_core_resources(
    tmp_path, epic_token_response
):
    response, fhir = await _read_resources(tmp_path, epic_token_response)

    assert response.status_code == 200
    assert _requests_made(fhir) == US_CORE_REQUESTS


@respx.mock
async def test_include_all_asks_for_the_long_tail_as_well(tmp_path, epic_token_response):
    response, fhir = await _read_resources(
        tmp_path, epic_token_response, params={"include": "all"}
    )

    assert response.status_code == 200
    made = _requests_made(fhir)
    assert len(fhir.calls) == len(RESOURCE_FETCH_CONFIG)
    assert US_CORE_REQUESTS <= made
    assert (f"{FHIR_PATH}/CarePlan", None) in made


@respx.mock
async def test_an_unrecognised_include_value_is_refused(tmp_path, epic_token_response):
    response, fhir = await _read_resources(
        tmp_path, epic_token_response, params={"include": "everything"}
    )

    assert response.status_code == 422
    assert not fhir.calls  # refused before any request reached the EHR


def test_every_us_core_name_exists_in_the_fetch_config():
    # The default tier names rows in RESOURCE_FETCH_CONFIG. A name that matches
    # nothing would silently shrink the default read, so it has to fail loudly.
    validate_us_core_membership(US_CORE_RESOURCES, RESOURCE_FETCH_CONFIG)  # shipped config

    with pytest.raises(ValueError):
        validate_us_core_membership(frozenset({"Cndition"}), RESOURCE_FETCH_CONFIG)


def test_the_fhir_type_of_a_row_comes_from_the_url_it_fetches():
    # A row's key is not always the resource type: the Observation searches are
    # split by category, and Patient is read by id rather than searched.
    assert fhir_type_for(RESOURCE_FETCH_CONFIG["ObservationVitalSigns"]) == "Observation"
    assert fhir_type_for(RESOURCE_FETCH_CONFIG["ObservationLaboratory"]) == "Observation"
    assert fhir_type_for(RESOURCE_FETCH_CONFIG["Patient"]) == "Patient"
    assert fhir_type_for(RESOURCE_FETCH_CONFIG["Condition"]) == "Condition"


def test_retired_pre_r4_resource_names_are_not_fetched():
    # MedicationOrder and ProcedureRequest are DSTU2/STU3 names that no R4 server
    # serves; their successors are what we ask for.
    assert "MedicationOrder" not in RESOURCE_FETCH_CONFIG
    assert "ProcedureRequest" not in RESOURCE_FETCH_CONFIG
    assert "MedicationRequest" in RESOURCE_FETCH_CONFIG
    assert "ServiceRequest" in RESOURCE_FETCH_CONFIG
