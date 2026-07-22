"""The edges of turning a provider's FHIR response into our own shape.

The cross-provider behaviour lives in ``test_normalized_resources_flow.py``.
What is pinned here is the awkward input: the three different bodies a server can
answer with, a resource that will not parse, and the value types an Observation
can carry. Those are the cases that decide whether a record renders or a request
falls over.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.fhir import normalize
from app.providers.config import RESOURCE_FETCH_CONFIG, US_CORE_RESOURCES, fhir_type_for
from tests.app_harness import load_fixture


def test_every_default_resource_has_a_summary_written_for_it():
    # The default tier and the summarizers both follow US Core, but nothing links
    # them, and _summarize_any makes a gap graceful rather than visible: a type
    # promoted into the default read without a summary would quietly return a
    # thin entry with an empty detail. Fail here instead.
    default_types = {fhir_type_for(RESOURCE_FETCH_CONFIG[name]) for name in US_CORE_RESOURCES}
    assert default_types <= normalize._SUMMARIZERS.keys()


ENVELOPE_KEYS = {
    "resourceType",
    "status",
    "statusCode",
    "count",
    "total",
    "truncated",
    "skipped",
    "error",
    "entries",
}
SUMMARY_KEYS = {
    "id",
    "resourceType",
    "title",
    "code",
    "status",
    "date",
    "category",
    "detail",
    "resource",
}


def bundle(*resources, total=None, next_link=False) -> dict:
    body = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
        "link": [{"relation": "self", "url": "https://example.org/fhir/Condition"}],
    }
    if total is not None:
        body["total"] = total
    if next_link:
        body["link"].append(
            {"relation": "next", "url": "https://example.org/fhir/Condition?page=2"}
        )
    return body


CONDITION = {
    "resourceType": "Condition",
    "id": "cond-1",
    "clinicalStatus": {
        "coding": [
            {
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": "active",
            }
        ]
    },
    "code": {
        "coding": [
            {"system": "http://snomed.info/sct", "code": "44054006", "display": "Diabetes"}
        ],
        "text": "Type 2 diabetes mellitus",
    },
    "subject": {"reference": "Patient/p1"},
    "onsetDateTime": "2019-03-04T00:00:00+00:00",
    "recordedDate": "2019-03-11T00:00:00+00:00",
}


def observation(**overrides) -> dict:
    base = {
        "resourceType": "Observation",
        "id": "obs-1",
        "status": "final",
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "718-7", "display": "Hemoglobin"}],
            "text": "Hemoglobin",
        },
        "subject": {"reference": "Patient/p1"},
        "effectiveDateTime": "2020-05-06T09:00:00+00:00",
    }
    base.update(overrides)
    return base


# --- the three bodies a server answers with ----------------------------------


def test_a_searchset_becomes_one_summary_per_entry():
    result = normalize.normalize_response(bundle(CONDITION), fhir_type="Condition")

    assert set(result) == ENVELOPE_KEYS
    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["skipped"] == 0

    summary = result["entries"][0]
    assert set(summary) == SUMMARY_KEYS
    assert summary["id"] == "cond-1"
    assert summary["title"] == "Type 2 diabetes mellitus"
    assert summary["code"] == {
        "system": "http://snomed.info/sct",
        "code": "44054006",
        "display": "Diabetes",
    }
    assert summary["status"] == "active"
    # Onset is when the patient got the condition; recordedDate is only when a
    # clinician typed it in, so onset wins where both are present.
    assert summary["date"].startswith("2019-03-04")


def test_a_bare_resource_is_normalized_like_a_single_entry_bundle():
    # Patient is fetched as /Patient/{id}, so it comes back as a resource, not a
    # bundle. The caller should not have to care which it was.
    patient = {
        "resourceType": "Patient",
        "id": "p1",
        "active": True,
        "name": [{"family": "Shaw", "given": ["Amy", "V"]}],
        "gender": "female",
        "birthDate": "2007-03-20",
    }

    result = normalize.normalize_response(patient, fhir_type="Patient")

    assert set(result) == ENVELOPE_KEYS
    assert result["count"] == 1
    assert result["total"] == 1
    assert result["truncated"] is False
    summary = result["entries"][0]
    assert summary["title"] == "Amy V Shaw"
    assert summary["date"] == "2007-03-20"
    assert summary["detail"]["gender"] == "female"


def test_an_operation_outcome_is_reported_as_an_error_not_an_empty_record():
    # A server can answer 200 with an OperationOutcome. Treating that as "no
    # resources found" would show the patient an empty record instead of a fault.
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "processing",
                "diagnostics": "Search parameter not supported",
            }
        ],
    }

    result = normalize.normalize_response(outcome, fhir_type="Condition")

    assert set(result) == ENVELOPE_KEYS
    assert result["status"] == "error"
    assert result["entries"] == []
    assert "Search parameter not supported" in result["error"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="empty-object"),
        pytest.param({"error": "rate limited"}, id="not-fhir-at-all"),
        pytest.param({"resourceType": 42}, id="resource-type-not-a-string"),
        pytest.param([], id="a-list"),
    ],
)
def test_a_body_that_is_json_but_not_fhir_is_an_error_not_a_crash(body):
    # A gateway or WAF can answer 200 with its own JSON. Nothing in it names a
    # resource type, and failing to notice that is the one input class that
    # escapes the per-resource skip machinery and would take down the whole read.
    result = normalize.normalize_response(body, fhir_type="Condition")

    assert set(result) == ENVELOPE_KEYS
    assert result["status"] == "error"
    assert result["entries"] == []
    assert result["error"]


def test_a_single_resource_of_the_wrong_type_is_an_error():
    # Unlike a bundle, where a foreign resource is just an entry that was not
    # asked for, a single-resource read that returns the wrong type returned
    # nothing that was asked for.
    result = normalize.normalize_response(
        {"resourceType": "Patient", "id": "p1"}, fhir_type="Condition"
    )

    assert result["status"] == "error"
    assert result["entries"] == []
    assert "Patient" in result["error"]


def test_a_non_numeric_total_is_dropped_rather_than_passed_through():
    # total is documented as a number. A server that sends a string should not
    # be able to change the type of a field the frontend binds to.
    result = normalize.normalize_response(
        {"resourceType": "Bundle", "type": "searchset", "total": "17", "entry": []},
        fhir_type="Condition",
    )

    assert result["total"] is None


def test_a_failed_fetch_keeps_the_same_envelope():
    # The frontend should never branch on shape, so a transport failure carries
    # the same keys as a successful read.
    result = normalize.failed_response(
        fhir_type="Immunization", error="Gateway Timeout", status_code=504
    )

    assert set(result) == ENVELOPE_KEYS
    assert result["status"] == "error"
    assert result["statusCode"] == 504
    assert result["entries"] == []
    assert result["count"] == 0


def test_a_fhir_error_body_is_reported_but_a_vendor_error_page_is_not():
    # An OperationOutcome says something useful and is worth passing on. An HTML
    # error page says nothing and would put a vendor's internals in our response.
    outcome = json.dumps(
        {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "diagnostics": "Scope patient/Condition.read denied"}],
        }
    )

    assert normalize.failure_message(403, outcome) == "Scope patient/Condition.read denied"
    assert normalize.failure_message(504, "<html><body>Gateway Timeout</body></html>") == (
        "Provider returned HTTP 504"
    )
    assert normalize.failure_message(None, None) == "Could not reach the provider"


def test_normalize_failure_wraps_a_raw_status_and_body_into_the_envelope():
    # The one call a fetch makes on a failed read: reason from the body, wrapped
    # in the same envelope a success uses.
    result = normalize.normalize_failure(
        fhir_type="Immunization",
        status_code=504,
        body="<html>Gateway Timeout</html>",
    )

    assert result["status"] == "error"
    assert result["statusCode"] == 504
    assert result["entries"] == []
    assert result["error"] == "Provider returned HTTP 504"


def test_an_empty_bundle_is_an_empty_record_not_an_error():
    result = normalize.normalize_response(bundle(total=0), fhir_type="AllergyIntolerance")

    assert result["status"] == "ok"
    assert result["entries"] == []
    assert result["count"] == 0
    assert result["total"] == 0


# --- partial pages -----------------------------------------------------------


def test_a_next_link_marks_the_record_as_truncated():
    result = normalize.normalize_response(
        bundle(CONDITION, total=17, next_link=True), fhir_type="Condition"
    )

    assert result["count"] == 1
    assert result["total"] == 17
    assert result["truncated"] is True


def test_truncation_is_reported_even_when_the_server_omits_a_total():
    # Bundle.total is optional and several servers leave it out, so the next link
    # is what "there is more" has to be read from.
    result = normalize.normalize_response(
        bundle(CONDITION, next_link=True), fhir_type="Condition"
    )

    assert result["total"] is None
    assert result["truncated"] is True


# --- one bad resource must not cost the rest ---------------------------------


def test_a_resource_that_will_not_parse_is_skipped_and_the_rest_survive(caplog):
    malformed = {"resourceType": "Condition", "id": "cond-bad", "code": "Diabetes"}

    with caplog.at_level(logging.WARNING):
        result = normalize.normalize_response(
            bundle(CONDITION, malformed), fhir_type="Condition"
        )

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["skipped"] == 1
    assert [e["id"] for e in result["entries"]] == ["cond-1"]

    assert "cond-bad" in caplog.text
    assert "Condition" in caplog.text


def test_a_summarizer_that_raises_skips_its_resource_rather_than_the_read(monkeypatch, caplog):
    # The parse guard drops one bad resource, but summarizing a validated model
    # is a second step, and a summarizer that raised there would otherwise take
    # down the whole read. Force one to raise and check only its resource is lost.
    def boom(_model):
        raise AttributeError("'NoneType' object has no attribute 'coding'")

    monkeypatch.setitem(normalize._SUMMARIZERS, "Condition", boom)

    good = {
        "resourceType": "AllergyIntolerance",
        "id": "allergy-1",
        "patient": {"reference": "Patient/p1"},
    }
    with caplog.at_level(logging.WARNING):
        conditions = normalize.normalize_response(bundle(CONDITION), fhir_type="Condition")
        allergies = normalize.normalize_response(bundle(good), fhir_type="AllergyIntolerance")

    assert conditions["status"] == "ok"
    assert conditions["count"] == 0
    assert conditions["skipped"] == 1
    assert "cond-1" in caplog.text
    # An unrelated type still summarizes normally.
    assert allergies["count"] == 1


def test_the_skipped_resource_warning_carries_no_patient_data(caplog):
    # A pydantic ValidationError renders the offending input in its message, so
    # logging the exception itself would put clinical values in the log. Only the
    # field path and the reason belong there.
    malformed = {
        "resourceType": "Condition",
        "id": "cond-bad",
        "code": "Metastatic neuroblastoma",
    }

    with caplog.at_level(logging.WARNING):
        normalize.normalize_response(bundle(malformed), fhir_type="Condition")

    assert "Metastatic neuroblastoma" not in caplog.text
    assert "code" in caplog.text  # the field path is named, its value is not


def test_a_bundle_entry_of_another_type_is_not_counted_as_a_resource():
    # A searchset can carry an OperationOutcome describing warnings about the
    # search itself, and resources pulled in alongside the matches. Neither is
    # something the caller asked for, and letting either through would put a
    # resource in the record that the patient does not have.
    body = bundle(CONDITION)
    body["entry"].append(
        {
            "resource": {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "warning", "code": "informational"}],
            },
            "search": {"mode": "outcome"},
        }
    )
    body["entry"].append(
        {
            "resource": {"resourceType": "Practitioner", "id": "prac-1"},
            "search": {"mode": "include"},
        }
    )

    result = normalize.normalize_response(body, fhir_type="Condition")

    assert [e["resourceType"] for e in result["entries"]] == ["Condition"]
    assert result["count"] == 1
    # Not failures, so they do not inflate the skipped count either.
    assert result["skipped"] == 0


def test_an_entry_without_a_resource_is_skipped():
    # Search bundles can carry entries that hold only a search mode or an
    # outcome, with no resource to normalize.
    body = bundle(CONDITION)
    body["entry"].append({"search": {"mode": "outcome"}})

    result = normalize.normalize_response(body, fhir_type="Condition")

    assert result["count"] == 1
    assert result["skipped"] == 1


# --- shape holds for types with no hand-written summary ----------------------


def test_an_unmapped_resource_type_still_gets_the_common_shape():
    # ?include=all reaches types with no summary of their own. They must still
    # come back with the same keys rather than a second shape to handle.
    care_plan = {
        "resourceType": "CarePlan",
        "id": "cp-1",
        "status": "active",
        "intent": "plan",
        "subject": {"reference": "Patient/p1"},
    }

    result = normalize.normalize_response(bundle(care_plan), fhir_type="CarePlan")

    summary = result["entries"][0]
    assert set(summary) == SUMMARY_KEYS
    assert summary["id"] == "cp-1"
    assert summary["status"] == "active"
    assert summary["resource"]["resourceType"] == "CarePlan"


def test_a_resource_with_only_its_required_fields_yields_nulls_not_errors():
    sparse = {"resourceType": "Condition", "id": "cond-2", "subject": {"reference": "Patient/p1"}}

    summary = normalize.normalize_response(bundle(sparse), fhir_type="Condition")["entries"][0]

    assert summary["title"] is None
    assert summary["code"] is None
    assert summary["status"] is None
    assert summary["date"] is None


def test_the_full_validated_resource_travels_with_the_summary():
    # Research consumers need what the summary leaves out, and dropping nulls is
    # what stops the key set varying with whatever the vendor sent.
    summary = normalize.normalize_response(bundle(CONDITION), fhir_type="Condition")["entries"][0]

    assert summary["resource"]["resourceType"] == "Condition"
    assert summary["resource"]["subject"] == {"reference": "Patient/p1"}
    assert "abatementDateTime" not in summary["resource"]


# --- Observation carries its result in one of several places -----------------


OBSERVATION_DETAIL_KEYS = {"value", "unit", "components", "interpretation", "referenceRange"}


def test_a_quantity_result_keeps_its_number_and_unit():
    obs = observation(
        valueQuantity={
            "value": 13.2,
            "unit": "g/dL",
            "system": "http://unitsofmeasure.org",
            "code": "g/dL",
        }
    )

    summary = normalize.normalize_response(bundle(obs), fhir_type="Observation")["entries"][0]

    assert summary["title"] == "Hemoglobin"
    assert summary["status"] == "final"
    assert set(summary["detail"]) == OBSERVATION_DETAIL_KEYS
    assert summary["detail"]["value"] == 13.2
    assert summary["detail"]["unit"] == "g/dL"


def test_a_quantity_falls_back_to_its_ucum_code_when_no_unit_is_spelled_out():
    # Quantity.unit is the human wording and is optional; some servers send only
    # the coded form.
    obs = observation(
        valueQuantity={"value": 4.1, "system": "http://unitsofmeasure.org", "code": "10*9/L"}
    )

    summary = normalize.normalize_response(bundle(obs), fhir_type="Observation")["entries"][0]

    assert summary["detail"]["unit"] == "10*9/L"


def test_a_lab_result_carries_the_flag_and_range_that_make_it_readable():
    # A number alone is not interpretable, and in pediatrics the normal range
    # moves with age, so the server's own range is what should travel with it.
    obs = observation(
        valueQuantity={"value": 9.8, "unit": "g/dL"},
        interpretation=[
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                        "code": "L",
                        "display": "Low",
                    }
                ]
            }
        ],
        referenceRange=[{"low": {"value": 11.5, "unit": "g/dL"}, "high": {"value": 15.5, "unit": "g/dL"}}],
    )

    summary = normalize.normalize_response(bundle(obs), fhir_type="Observation")["entries"][0]

    assert summary["detail"]["interpretation"] == "Low"
    # Structured rather than rendered, for the same reason value is: a consumer
    # flagging out-of-range results should not have to parse "11.5 - 15.5 g/dL".
    assert summary["detail"]["referenceRange"] == {
        "low": 11.5,
        "high": 15.5,
        "unit": "g/dL",
        "text": None,
    }


def test_a_reference_range_keeps_the_servers_own_wording_where_it_gave_one():
    obs = observation(
        valueQuantity={"value": 9.8, "unit": "g/dL"},
        referenceRange=[{"high": {"value": 15.5, "unit": "g/dL"}, "text": "under 15.5 for age"}],
    )

    summary = normalize.normalize_response(bundle(obs), fhir_type="Observation")["entries"][0]

    assert summary["detail"]["referenceRange"] == {
        "low": None,
        "high": 15.5,
        "unit": "g/dL",
        "text": "under 15.5 for age",
    }


def test_a_coded_result_reads_as_text():
    obs = observation(
        valueCodeableConcept={
            "coding": [{"system": "http://snomed.info/sct", "code": "10828004"}],
            "text": "Positive",
        }
    )

    summary = normalize.normalize_response(bundle(obs), fhir_type="Observation")["entries"][0]

    assert summary["detail"]["value"] == "Positive"
    assert summary["detail"]["unit"] is None


def test_a_blood_pressure_reports_its_components_because_it_has_no_value_of_its_own():
    # Blood pressure is a panel: the parent Observation carries no value[x] at
    # all, and the two numbers live in components.
    obs = observation(
        code={
            "coding": [{"system": "http://loinc.org", "code": "85354-9"}],
            "text": "Blood pressure",
        },
        component=[
            {
                "code": {
                    "coding": [
                        {"system": "http://loinc.org", "code": "8480-6", "display": "Systolic"}
                    ]
                },
                "valueQuantity": {"value": 108, "unit": "mm[Hg]"},
            },
            {
                "code": {
                    "coding": [
                        {"system": "http://loinc.org", "code": "8462-4", "display": "Diastolic"}
                    ]
                },
                "valueQuantity": {"value": 72, "unit": "mm[Hg]"},
            },
        ],
    )

    summary = normalize.normalize_response(bundle(obs), fhir_type="Observation")["entries"][0]

    # Left as None rather than synthesised into "108/72": value is the
    # measurement, and inventing one would put a display string where consumers
    # read a number. The pair is in components, faithfully.
    assert summary["detail"]["value"] is None

    # Each component keeps its own code. Servers order components as they like
    # (the launcher really does put diastolic first) and word the labels
    # differently, so the LOINC code is the only stable way to tell the two
    # numbers apart.
    assert summary["detail"]["components"] == [
        {
            "title": "Systolic",
            "code": {"system": "http://loinc.org", "code": "8480-6", "display": "Systolic"},
            "value": 108,
            "unit": "mm[Hg]",
        },
        {
            "title": "Diastolic",
            "code": {"system": "http://loinc.org", "code": "8462-4", "display": "Diastolic"},
            "value": 72,
            "unit": "mm[Hg]",
        },
    ]


def test_an_observation_with_no_result_at_all_still_normalizes():
    summary = normalize.normalize_response(
        bundle(observation()), fhir_type="Observation"
    )["entries"][0]

    assert summary["detail"]["value"] is None
    assert summary["detail"]["unit"] is None
    assert summary["detail"]["components"] == []


# --- real captured responses -------------------------------------------------


@pytest.mark.parametrize("fixture", ["launcher_patient_record.json", "cerner_patient_record.json"])
def test_every_captured_response_normalizes_without_raising(fixture):
    # Whatever a real server sent, the caller gets an envelope back.
    record = load_fixture(fixture)

    for key, response in record["responses"].items():
        fhir_type = "Observation" if key.startswith("Observation") else key
        if response["statusCode"] == 200:
            result = normalize.normalize_response(response["body"], fhir_type=fhir_type)
        else:
            result = normalize.failed_response(
                fhir_type=fhir_type, error="upstream failure", status_code=response["statusCode"]
            )
        assert set(result) == ENVELOPE_KEYS, key
        for summary in result["entries"]:
            assert set(summary) == SUMMARY_KEYS, key
