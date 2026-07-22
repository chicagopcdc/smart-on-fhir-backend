"""Reading a record end to end, against two servers that answer differently.

The whole point of normalizing is that a caller should not be able to tell which
vendor a record came from. So the same flow — authorize, exchange, read — runs
twice against real captured responses from two R4 servers, and what comes out the
far end is compared.

Discovery, the token endpoint and the FHIR searches are served by respx from
fixtures; everything between them is the real application.
"""

from __future__ import annotations

import copy
import json
import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.providers.config import RESOURCE_FETCH_CONFIG, fhir_type_for
from tests.app_harness import (
    CERNER_SANDBOX,
    SMART_LAUNCHER,
    load_fixture,
    read_resources,
)


# Which fetch config row a FHIR request came from, keyed the way the request
# identifies itself: the resource type it addresses plus the `category` that
# separates the Observation searches. Inverted from the config rather than
# rebuilt from a naming convention, so a row the config spells differently still
# resolves.
FETCH_KEYS = {
    (fhir_type_for(entry), (entry.get("extra_params") or {}).get("category")): name
    for name, entry in RESOURCE_FETCH_CONFIG.items()
}
FHIR_TYPES = {fhir_type for fhir_type, _ in FETCH_KEYS}

LAUNCHER = {**SMART_LAUNCHER, "id": "launcher", "record": "launcher_patient_record.json"}
# The record was captured from Cerner's open endpoint, which is the
# unauthenticated view of the same tenant as the configured issuer, so the data
# is Oracle Health's while the URL is the one the allowlist authorizes.
CERNER = {**CERNER_SANDBOX, "id": "cerner", "record": "cerner_patient_record.json"}
SERVERS = [pytest.param(LAUNCHER, id="launcher"), pytest.param(CERNER, id="cerner")]


def _token_response(patient_id: str) -> dict:
    return {
        "access_token": "PLACEHOLDER.access.jwt",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "launch/patient patient/*.read",
        "patient": patient_id,
    }


def _fetch_key(url) -> str | None:
    """The fetch config row a FHIR request came from."""
    parsed = urlparse(str(url))
    resource = next(
        (segment for segment in reversed(parsed.path.split("/")) if segment in FHIR_TYPES),
        None,
    )
    category = parse_qs(parsed.query).get("category", [None])[0]
    return FETCH_KEYS.get((resource, category))


def _serve_record(record: dict):
    """Answer each FHIR search with what that server really returned."""

    def responder(request):
        response = record["responses"].get(_fetch_key(request.url))
        if response is None:
            return httpx.Response(404, json={"resourceType": "OperationOutcome", "issue": []})
        return httpx.Response(response["statusCode"], json=response["body"])

    return responder


async def read_record(server: dict, tmp_path, *, record=None, params=None) -> dict:
    """Authorize against a server and read the patient's record back."""
    record = record or load_fixture(server["record"])
    response, _ = await read_resources(
        server,
        f"sqlite+aiosqlite:///{tmp_path / (server['id'] + '.db')}",
        token_response=_token_response(record["patientId"]),
        responder=_serve_record(record),
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


def summary_shape(summary: dict) -> tuple:
    """The keys a summary presents, which is what a consumer binds to.

    The validated resource underneath is the vendor's own structure and is
    expected to differ; the summary is the part that must not.
    """
    return tuple(sorted(summary)), tuple(sorted(summary["detail"]))


# --- the cross-provider claim ------------------------------------------------


@respx.mock
async def test_both_servers_answer_in_the_same_shape(tmp_path):
    launcher = await read_record(LAUNCHER, tmp_path)
    cerner = await read_record(CERNER, tmp_path)

    assert launcher["resources"].keys() == cerner["resources"].keys()

    shared = [
        key
        for key in launcher["resources"]
        if launcher["resources"][key]["entries"] and cerner["resources"][key]["entries"]
    ]
    assert len(shared) >= 6, f"too few shared types to be a real comparison: {shared}"

    for key in shared:
        left, right = launcher["resources"][key], cerner["resources"][key]
        assert left.keys() == right.keys(), key
        for a in left["entries"]:
            for b in right["entries"]:
                assert summary_shape(a) == summary_shape(b), key


@respx.mock
async def test_the_two_servers_really_do_answer_differently(tmp_path):
    # Guards the test above from being vacuous: if the raw payloads were already
    # alike there would be nothing for normalization to do.
    launcher = load_fixture(LAUNCHER["record"])
    cerner = load_fixture(CERNER["record"])

    def raw_keys(record, key):
        return set(record["responses"][key]["body"]["entry"][0]["resource"])

    assert raw_keys(launcher, "Condition") != raw_keys(cerner, "Condition")
    assert raw_keys(launcher, "Encounter") != raw_keys(cerner, "Encounter")


@respx.mock
@pytest.mark.parametrize("server", SERVERS)
async def test_a_record_is_readable_whichever_server_it_came_from(server, tmp_path):
    # Shape alone proves little if the slots are empty: this is the claim that
    # the labels and dates were actually found in two different layouts.
    record = await read_record(server, tmp_path)

    assert record["include"] == "us-core"
    assert record["provider"] == server["provider"]
    assert record["iss"] == server["iss"]

    patient = record["resources"]["Patient"]
    assert patient["count"] == 1
    assert patient["entries"][0]["title"]
    assert patient["entries"][0]["detail"]["birthDate"]

    for key in ("Condition", "ObservationLaboratory"):
        entries = record["resources"][key]["entries"]
        assert entries, key
        for entry in entries:
            assert entry["title"], key
            assert entry["code"]["code"], key
            assert entry["date"], key


# --- partial and failed reads ------------------------------------------------


@respx.mock
async def test_a_resource_the_server_could_not_return_does_not_sink_the_record(tmp_path):
    # Cerner's Immunization search really did time out while the record was
    # captured, so this is a genuine partial read rather than a contrived one.
    record = await read_record(CERNER, tmp_path)

    immunization = record["resources"]["Immunization"]
    assert immunization["status"] == "error"
    assert immunization["statusCode"] == 504
    assert immunization["entries"] == []

    assert record["resources"]["Condition"]["entries"]


@respx.mock
async def test_a_malformed_resource_is_skipped_and_logged_not_raised(tmp_path, caplog):
    record = copy.deepcopy(load_fixture(LAUNCHER["record"]))
    entries = record["responses"]["Condition"]["body"]["entry"]
    good = len(entries)
    entries.append(
        {"resource": {"resourceType": "Condition", "id": "cond-broken", "code": "Asthma"}}
    )

    with caplog.at_level(logging.WARNING):
        result = await read_record(LAUNCHER, tmp_path, record=record)

    condition = result["resources"]["Condition"]
    assert condition["status"] == "ok"
    assert condition["count"] == good
    assert condition["skipped"] == 1
    assert "cond-broken" in caplog.text
    assert "Asthma" not in caplog.text


# --- the tiers, through the endpoint -----------------------------------------


@respx.mock
async def test_the_long_tail_arrives_in_the_same_envelope_as_the_default(tmp_path):
    default = await read_record(LAUNCHER, tmp_path)
    everything = await read_record(LAUNCHER, tmp_path, params={"include": "all"})

    assert everything["include"] == "all"
    assert default["resources"].keys() < everything["resources"].keys()

    envelope_keys = set(next(iter(default["resources"].values())))
    for envelope in everything["resources"].values():
        assert set(envelope) == envelope_keys

    # A type with no summary of its own falls back to the generic one, and has
    # to arrive in the same shape as the rest rather than as a second thing to
    # handle. CarePlan is in the fixture for exactly this.
    care_plan = everything["resources"]["CarePlan"]
    assert care_plan["status"] == "ok"
    assert care_plan["entries"]
    for entry in care_plan["entries"]:
        assert set(entry) == set(default["resources"]["Condition"]["entries"][0])
        assert entry["resourceType"] == "CarePlan"
        assert entry["status"]


@respx.mock
async def test_the_whole_response_is_json_serializable(tmp_path):
    # The models parse FHIR decimals as Decimal, which json refuses, so a lab
    # result is the kind of value that would break serialization silently.
    record = await read_record(LAUNCHER, tmp_path, params={"include": "all"})

    json.dumps(record)
