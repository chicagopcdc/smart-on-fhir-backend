"""The API a downstream consumer integrates against, end to end.

Connect a provider, read the record back, summarize it — through the real
endpoints, against captured responses from two R4 servers that genuinely answer
differently. Then the part that only exists because a record can span providers:
connect a second one, and check that a summary still arrives when one of them is
down.

Discovery, the token endpoint and the FHIR searches are served by respx from
fixtures; everything between them is the real application.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.fhir import summary as summary_module
from tests.app_harness import (
    CERNER_SANDBOX as CERNER,
    SMART_LAUNCHER as LAUNCHER,
    age_tokens,
    app_db,
    client,
    connect,
    load_fixture,
    mock_server,
    serve_record,
    token_endpoint,
    token_response,
)


def _serve(server: dict, responder) -> None:
    """Answer this server's FHIR calls, after its auth routes are registered."""
    respx.get(url__startswith=server["iss"]).mock(side_effect=responder)


async def _connect_server(http, server: dict, *, link_session: str | None = None) -> dict:
    """Authorize one server and start serving its captured record."""
    record = load_fixture(server["record"])
    body = await connect(
        http,
        server,
        token_response(record["patientId"]),
        link_session=link_session,
    )
    _serve(server, serve_record(record))
    return body


def _auth(session_id: str) -> dict:
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'downstream.db'}"


# --- reading a record --------------------------------------------------------


@respx.mock
async def test_a_connected_patient_reads_their_record(db_url):
    async with app_db(db_url):
        async with client() as http:
            connected = await _connect_server(http, LAUNCHER)
            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=_auth(connected["sessionId"]),
            )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["patientId"] == connected["patientId"]
    assert body["include"] == "us-core"

    [connection] = body["connections"]
    assert connection["provider"] == LAUNCHER["provider"]
    assert connection["patientFhirId"] == connected["connection"]["patientFhirId"]
    assert connection["status"] == "ok"

    # The envelope the normalization layer produces, arriving intact through the
    # response model rather than flattened by it.
    condition = connection["resources"]["Condition"]
    assert condition["resourceType"] == "Condition"
    assert condition["status"] == "ok"
    assert condition["count"] == len(condition["entries"])
    assert condition["entries"][0]["resource"]["resourceType"] == "Condition"


@respx.mock
async def test_a_read_can_name_the_types_it_wants(db_url):
    async with app_db(db_url):
        async with client() as http:
            connected = await _connect_server(http, LAUNCHER)
            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                params=[("type", "Condition"), ("type", "Immunization")],
                headers=_auth(connected["sessionId"]),
            )

    body = response.json()
    assert body["types"] == ["Condition", "Immunization"]
    assert set(body["connections"][0]["resources"]) == {"Condition", "Immunization"}


@respx.mock
async def test_a_type_the_backend_does_not_fetch_is_refused(db_url):
    async with app_db(db_url):
        async with client() as http:
            connected = await _connect_server(http, LAUNCHER)
            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                params={"type": "NotAResourceType"},
                headers=_auth(connected["sessionId"]),
            )

    # A 422 naming the types that exist, rather than an empty result that looks
    # like the patient simply has none of it.
    assert response.status_code == 422


# --- the summary -------------------------------------------------------------


@respx.mock
async def test_a_summary_reads_as_a_chart(db_url):
    async with app_db(db_url):
        async with client() as http:
            connected = await _connect_server(http, LAUNCHER)
            response = await http.get(
                f"/patients/{connected['patientId']}/summary",
                headers=_auth(connected["sessionId"]),
            )

    assert response.status_code == 200, response.text
    body = response.json()

    # Every section, in the same order, whether or not this patient has any.
    assert [section["key"] for section in body["sections"]] == [
        key for key, _, _ in summary_module.SECTIONS
    ]
    assert body["demographics"]["sources"] == [LAUNCHER["provider"]]
    assert body["demographics"]["birthDate"]

    populated = [s for s in body["sections"] if s["items"]]
    assert len(populated) >= 4, "the captured record should fill several sections"
    for section in populated:
        assert section["returned"] == len(section["items"])
        assert section["total"] >= section["returned"]
        # Provenance survives the merge, even with one provider connected.
        assert {item["provider"] for item in section["items"]} == {LAUNCHER["provider"]}


@respx.mock
async def test_a_summary_orders_each_section_newest_first(db_url):
    async with app_db(db_url):
        async with client() as http:
            connected = await _connect_server(http, LAUNCHER)
            response = await http.get(
                f"/patients/{connected['patientId']}/summary",
                headers=_auth(connected["sessionId"]),
            )

    for section in response.json()["sections"]:
        dated = [item["date"] for item in section["items"] if item["date"]]
        undated = [item["date"] for item in section["items"] if not item["date"]]
        assert dated == sorted(dated, reverse=True), section["key"]
        # Undated resources are unknown rather than newest, so they sort last and
        # cannot push real findings out of a capped section.
        if undated:
            assert section["items"][-1]["date"] is None, section["key"]


@respx.mock
async def test_limit_caps_the_items_without_hiding_the_count(db_url):
    async with app_db(db_url):
        async with client() as http:
            connected = await _connect_server(http, LAUNCHER)
            full = await http.get(
                f"/patients/{connected['patientId']}/summary",
                headers=_auth(connected["sessionId"]),
            )
            capped = await http.get(
                f"/patients/{connected['patientId']}/summary",
                params={"limit": 1},
                headers=_auth(connected["sessionId"]),
            )

    by_key = {s["key"]: s for s in full.json()["sections"]}
    for section in capped.json()["sections"]:
        assert len(section["items"]) <= 1
        assert section["returned"] == len(section["items"])
        # The cap is on what is returned, not on what is reported to exist.
        assert section["total"] == by_key[section["key"]]["total"]


# --- more than one provider --------------------------------------------------


@respx.mock
async def test_a_summary_merges_the_records_two_providers_hold(db_url):
    async with app_db(db_url):
        async with client() as http:
            first = await _connect_server(http, LAUNCHER)
            await _connect_server(http, CERNER, link_session=first["sessionId"])

            response = await http.get(
                f"/patients/{first['patientId']}/summary",
                headers=_auth(first["sessionId"]),
            )

    body = response.json()
    assert {c["provider"] for c in body["connections"]} == {
        LAUNCHER["provider"],
        CERNER["provider"],
    }

    providers_seen = {
        item["provider"] for section in body["sections"] for item in section["items"]
    }
    assert providers_seen == {LAUNCHER["provider"], CERNER["provider"]}, (
        "a merged summary should carry findings from both connections"
    )


@respx.mock
async def test_the_record_read_keeps_the_two_providers_apart(db_url):
    """Resources stay per connection: two servers, two sets, no invented join."""
    async with app_db(db_url):
        async with client() as http:
            first = await _connect_server(http, LAUNCHER)
            second = await _connect_server(http, CERNER, link_session=first["sessionId"])

            response = await http.get(
                f"/patients/{first['patientId']}/resources",
                headers=_auth(first["sessionId"]),
            )

    connections = response.json()["connections"]
    assert len(connections) == 2
    by_provider = {c["provider"]: c for c in connections}
    assert (
        by_provider[LAUNCHER["provider"]]["patientFhirId"]
        != by_provider[CERNER["provider"]]["patientFhirId"]
    ), "the two servers identify this person differently, which is the whole point"
    assert second["patientId"] == first["patientId"]


@respx.mock
async def test_narrowing_to_one_provider_reads_only_that_one(db_url):
    async with app_db(db_url):
        async with client() as http:
            first = await _connect_server(http, LAUNCHER)
            await _connect_server(http, CERNER, link_session=first["sessionId"])

            narrowed = await http.get(
                f"/patients/{first['patientId']}/resources",
                params={"provider": CERNER["provider"]},
                headers=_auth(first["sessionId"]),
            )
            # A provider this record has not connected is a question with an
            # answer, not a missing record: the filter matches nothing and the
            # response says so.
            absent = await http.get(
                f"/patients/{first['patientId']}/resources",
                params={"provider": "EPIC_SANDBOX"},
                headers=_auth(first["sessionId"]),
            )

    assert [c["provider"] for c in narrowed.json()["connections"]] == [CERNER["provider"]]
    assert absent.status_code == 200
    assert absent.json()["connections"] == []


@respx.mock
async def test_a_provider_being_down_does_not_sink_the_summary(db_url):
    async with app_db(db_url):
        async with client() as http:
            first = await _connect_server(http, LAUNCHER)

            # Cerner authorizes, then stops answering: the state a connection
            # lands in when a provider has an outage after being connected.
            cerner_record = load_fixture(CERNER["record"])
            mock_server(CERNER, token_response(cerner_record["patientId"]))
            await connect(
                http,
                CERNER,
                token_response(cerner_record["patientId"]),
                link_session=first["sessionId"],
            )
            _serve(CERNER, lambda request: httpx.Response(503, text="down for maintenance"))

            response = await http.get(
                f"/patients/{first['patientId']}/summary",
                headers=_auth(first["sessionId"]),
            )

    # Still a normal answer, not an error: one provider's outage is not the
    # patient's whole record.
    assert response.status_code == 200
    body = response.json()

    health = {c["provider"]: c for c in body["connections"]}
    assert health[LAUNCHER["provider"]]["status"] == "ok"
    assert health[CERNER["provider"]]["status"] == "error"

    # What could not be read is named, so a partial summary is visibly partial
    # rather than looking like a patient with nothing on file.
    down = [issue for issue in body["issues"] if issue["provider"] == CERNER["provider"]]
    assert down and all("503" in issue["error"] for issue in down)

    # And the provider that is up still fills the chart.
    assert any(
        item["provider"] == LAUNCHER["provider"]
        for section in body["sections"]
        for item in section["items"]
    )


@respx.mock
async def test_one_failing_resource_type_leaves_the_connection_degraded(db_url):
    """A refusal on one type is not an outage, and is reported as neither."""
    record = load_fixture(LAUNCHER["record"])
    record["responses"]["Immunization"] = {
        "statusCode": 403,
        "body": {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "diagnostics": "Scope not granted"}],
        },
    }

    async with app_db(db_url):
        async with client() as http:
            body = await connect(http, LAUNCHER, token_response(record["patientId"]))
            _serve(LAUNCHER, serve_record(record))

            response = await http.get(
                f"/patients/{body['patientId']}/summary",
                headers=_auth(body["sessionId"]),
            )

    summary_body = response.json()
    assert summary_body["connections"][0]["status"] == "degraded"
    assert {"provider": LAUNCHER["provider"], "type": "Immunization",
            "error": "Scope not granted"} in summary_body["issues"]


# --- who may read what -------------------------------------------------------


@respx.mock
async def test_a_session_cannot_read_another_patients_record(db_url):
    async with app_db(db_url):
        async with client() as http:
            mine = await _connect_server(http, LAUNCHER)
            # A second, unlinked authorization: a different person entirely.
            theirs = await connect(http, CERNER, token_response("someone-else"))

            for path in ("resources", "summary"):
                response = await http.get(
                    f"/patients/{theirs['patientId']}/{path}",
                    headers=_auth(mine["sessionId"]),
                )
                # 404, not 403: confirming the id exists would itself leak.
                assert response.status_code == 404, path
                assert response.json()["detail"] == "No such patient record"


@respx.mock
async def test_a_second_caller_on_a_shared_account_reads_only_their_own(db_url):
    """The record boundary, seen through the endpoint that enforces it.

    Someone authorizes an account another record already holds, presenting no
    session. They land on a record of their own, so the read shows the one
    connection they authorized and not the providers the first caller linked
    alongside it.
    """
    async with app_db(db_url):
        async with client() as http:
            holder = await _connect_server(http, LAUNCHER)
            await _connect_server(http, CERNER, link_session=holder["sessionId"])

            # The same launcher account again, with no session presented.
            record = load_fixture(LAUNCHER["record"])
            other = await connect(http, LAUNCHER, token_response(record["patientId"]))

            reachable = await http.get(
                f"/patients/{other['patientId']}/resources",
                headers=_auth(other["sessionId"]),
            )

    assert other["patientId"] != holder["patientId"]
    assert {c["provider"] for c in reachable.json()["connections"]} == {
        LAUNCHER["provider"]
    }, "the second caller reached a provider they never authorized"


@respx.mock
async def test_reading_without_a_session_is_refused(db_url):
    async with app_db(db_url):
        async with client() as http:
            connected = await _connect_server(http, LAUNCHER)
            patient_id = connected["patientId"]

            missing = await http.get(f"/patients/{patient_id}/summary")
            malformed = await http.get(
                f"/patients/{patient_id}/summary",
                headers={"Authorization": "Basic bm90LWEtYmVhcmVy"},
            )
            unknown = await http.get(
                f"/patients/{patient_id}/resources", headers=_auth("not-a-session")
            )

    assert missing.status_code == 401
    assert malformed.status_code == 401
    assert unknown.status_code == 401


# --- a grant narrower than the request ----------------------------------------

# Three of the nine FHIR types the default tier reads. The launcher asks for
# `patient/*.read`, so this is a consent screen where someone ticked a subset,
# which is the ordinary way a narrowing happens.
NARROW_SCOPE = (
    "launch/patient openid offline_access "
    "patient/Patient.read patient/Condition.read patient/Observation.read"
)
WITHHELD = {
    "AllergyIntolerance",
    "DiagnosticReport",
    "Encounter",
    "Immunization",
    "MedicationRequest",
    "Procedure",
}


@respx.mock
async def test_a_narrowed_grant_reads_what_it_was_given_and_says_so(db_url):
    """A partial grant is a connection that works partly, not nine mysteries.

    Before this, the stored scope was written and never read: every withheld type
    was requested anyway, refused with a 403, and reported as a failed read with
    no explanation, on every read, forever.
    """
    record = load_fixture(LAUNCHER["record"])
    async with app_db(db_url):
        async with client() as http:
            body = await connect(
                http,
                LAUNCHER,
                token_response(record["patientId"], scope=NARROW_SCOPE),
            )
            route = respx.get(url__startswith=LAUNCHER["iss"]).mock(
                side_effect=serve_record(record)
            )

            response = await http.get(
                f"/patients/{body['patientId']}/resources",
                headers=_auth(body["sessionId"]),
            )

    assert response.status_code == 200, response.text
    [connection] = response.json()["connections"]
    assert connection["status"] == "degraded"

    resources = connection["resources"]
    # Every requested type is still reported, so a consumer needs no new branch to
    # find out something is missing.
    assert set(resources) == set(response.json()["types"])

    for name, envelope in resources.items():
        if name in WITHHELD:
            assert envelope["status"] == "error", name
            # Null rather than 403: nothing was asked, so claiming the provider
            # refused would put words in its mouth.
            assert envelope["statusCode"] is None, name
            assert "not granted" in envelope["error"], name
        else:
            assert envelope["status"] == "ok", name

    # And the requests were never spent. What a withheld type would have cost is
    # a round trip and a 403, on every read.
    requested = {str(call.request.url) for call in route.calls}
    for withheld in WITHHELD:
        assert not any(withheld in url for url in requested), (
            f"{withheld} was fetched from a connection not granted it"
        )
    assert any("Condition" in url for url in requested), (
        "nothing was fetched at all, so this proves nothing about what was skipped"
    )


@respx.mock
async def test_a_narrowed_grant_names_what_is_missing_in_the_summary(db_url):
    """The merged view has to be visibly partial rather than look like a thin chart."""
    record = load_fixture(LAUNCHER["record"])
    async with app_db(db_url):
        async with client() as http:
            body = await connect(
                http,
                LAUNCHER,
                token_response(record["patientId"], scope=NARROW_SCOPE),
            )
            _serve(LAUNCHER, serve_record(record))

            response = await http.get(
                f"/patients/{body['patientId']}/summary",
                headers=_auth(body["sessionId"]),
            )

    summary_body = response.json()
    assert summary_body["connections"][0]["status"] == "degraded"

    named = {
        issue["type"]
        for issue in summary_body["issues"]
        if "not granted" in issue["error"]
    }
    assert WITHHELD <= named, f"the summary does not say what was withheld: {named}"

    # What was granted still fills the chart.
    assert any(section["items"] for section in summary_body["sections"])


@respx.mock
async def test_a_grant_covering_nothing_asks_for_consent_again(db_url):
    """The one narrowing where reconnecting is genuinely the fix."""
    record = load_fixture(LAUNCHER["record"])
    async with app_db(db_url):
        async with client() as http:
            body = await connect(
                http,
                LAUNCHER,
                token_response(
                    record["patientId"], scope="launch/patient openid patient/Device.read"
                ),
            )
            route = respx.get(url__startswith=LAUNCHER["iss"]).mock(
                side_effect=serve_record(record)
            )

            response = await http.get(
                f"/patients/{body['patientId']}/resources",
                headers=_auth(body["sessionId"]),
            )

    assert response.status_code == 200
    [connection] = response.json()["connections"]
    assert connection["status"] == "error"
    assert connection["needsReauthorization"] is True
    assert not route.calls, "a connection granted nothing still spent requests"


@respx.mock
async def test_a_server_that_states_no_scope_is_read_in_full(db_url):
    """RFC 6749 §5.1: an omitted scope means the request was granted as asked.

    Reading it as "granted nothing" would turn one quiet server into a record that
    looks empty, which is the worst way to be wrong about this.
    """
    record = load_fixture(LAUNCHER["record"])
    granted = token_response(record["patientId"])
    granted.pop("scope")

    async with app_db(db_url):
        async with client() as http:
            body = await connect(http, LAUNCHER, granted)
            _serve(LAUNCHER, serve_record(record))

            response = await http.get(
                f"/patients/{body['patientId']}/resources",
                headers=_auth(body["sessionId"]),
            )

    [connection] = response.json()["connections"]
    assert connection["status"] == "ok"
    assert connection["resources"]["Immunization"]["status"] == "ok"


@respx.mock
async def test_a_grant_that_permits_no_reading_is_not_read_as_unrestricted(db_url):
    """Naming a resource and permitting nothing on it is a restriction, not silence.

    ``granted_types`` answers None for a grant it cannot act on, which has to
    cover a server describing only the session — but a grant of
    ``patient/Condition.c`` has named a resource and withheld reading it. Folding
    that into "unrestricted" would fetch every type and collect a 403 for each,
    which is the behaviour reading the scope at all exists to end.
    """
    record = load_fixture(LAUNCHER["record"])
    async with app_db(db_url):
        async with client() as http:
            body = await connect(
                http,
                LAUNCHER,
                token_response(
                    record["patientId"],
                    scope="launch/patient openid patient/Condition.c",
                ),
            )
            route = respx.get(url__startswith=LAUNCHER["iss"]).mock(
                side_effect=serve_record(record)
            )

            response = await http.get(
                f"/patients/{body['patientId']}/resources",
                headers=_auth(body["sessionId"]),
            )

    [connection] = response.json()["connections"]
    assert connection["status"] == "error"
    assert not route.calls, "a grant permitting no reads still spent requests"


@respx.mock
async def test_a_refresh_that_narrows_the_grant_is_honoured_on_the_same_read(db_url):
    """The scope a read obeys is the one the token it is using was issued under.

    A refresh commits in a session of its own, so the connection instance this
    request is holding still shows the wider scope it had a moment ago. Reading
    from that would ask for types the new token does not cover and report the
    refusals as the provider's fault.
    """
    record = load_fixture(LAUNCHER["record"])
    narrowed = "launch/patient offline_access patient/Patient.read patient/Condition.read"

    async with app_db(db_url) as factory:
        async with client() as http:
            body = await connect(
                http,
                LAUNCHER,
                token_endpoint(
                    record["patientId"],
                    on_refresh=lambda granted: httpx.Response(
                        200, json=granted(scope=narrowed)
                    ),
                ),
            )
            route = respx.get(url__startswith=LAUNCHER["iss"]).mock(
                side_effect=serve_record(record)
            )
            # Due for renewal, so the read refreshes before it fans out.
            await age_tokens(factory)

            response = await http.get(
                f"/patients/{body['patientId']}/resources",
                headers=_auth(body["sessionId"]),
            )

    assert response.status_code == 200, response.text
    [connection] = response.json()["connections"]
    assert connection["status"] == "degraded"

    withheld = connection["resources"]["Immunization"]
    assert withheld["status"] == "error"
    assert "not granted" in withheld["error"], (
        "the read obeyed the scope from before the refresh"
    )
    requested = {str(call.request.url) for call in route.calls}
    assert not any("Immunization" in url for url in requested)
    assert connection["resources"]["Condition"]["status"] == "ok"
