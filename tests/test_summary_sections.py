"""Folding envelopes into sections: the edges a captured record does not reach.

The flow tests drive this through real responses from two servers, which is what
proves it works. These cover the cases those fixtures happen not to contain — a
server that omits Bundle.total, two providers disagreeing about a patient, a
section with nothing in it — where the behaviour is a decision rather than a
mechanism.
"""

from __future__ import annotations

from app.fhir import normalize, summary
from app.providers import config


def _envelope(fhir_type: str, entries: list[dict], *, total: int | None = None) -> dict:
    """A successful read, built the way normalize builds one."""
    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": entry} for entry in entries],
    }
    if total is not None:
        bundle["total"] = total
    return normalize.normalize_response(bundle, fhir_type=fhir_type)


def _condition(code: str, display: str, date: str | None) -> dict:
    resource = {
        "resourceType": "Condition",
        "id": code,
        # subject is required in R4B, and the models enforce it: a resource
        # without one is skipped rather than summarized.
        "subject": {"reference": "Patient/p1"},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": code,
                             "display": display}]},
    }
    if date:
        resource["onsetDateTime"] = date
    return resource


def _patient(name: str, gender: str, birth_date: str) -> dict:
    return {
        "resourceType": "Patient",
        "id": "p1",
        "name": [{"use": "official", "family": name}],
        "gender": gender,
        "birthDate": birth_date,
    }


def test_every_section_is_present_even_when_the_patient_has_nothing():
    sections = summary.build_sections([("EPIC", {})], limit=20)

    assert [section["key"] for section in sections] == [
        key for key, _, _ in summary.SECTIONS
    ]
    # A consumer reads a fixed shape rather than probing for keys.
    assert all(section["items"] == [] and section["total"] == 0 for section in sections)


def test_a_section_reports_what_the_server_holds_not_what_fitted():
    entries = [_condition(str(n), f"Condition {n}", f"20{n:02d}-01-01") for n in range(5)]
    resources = {"Condition": _envelope("Condition", entries, total=41)}

    [conditions] = [
        s for s in summary.build_sections([("EPIC", resources)], limit=2)
        if s["key"] == "conditions"
    ]

    # One page was read and the server says there are 41; returning 2 of them
    # must not read as the patient having 2.
    assert conditions["total"] == 41
    assert conditions["returned"] == 2
    assert len(conditions["items"]) == 2


def test_a_server_that_omits_bundle_total_falls_back_to_what_arrived():
    entries = [_condition("1", "One", "2020-01-01"), _condition("2", "Two", "2021-01-01")]
    resources = {"Condition": _envelope("Condition", entries)}

    [conditions] = [
        s for s in summary.build_sections([("EPIC", resources)], limit=20)
        if s["key"] == "conditions"
    ]

    assert conditions["total"] == 2


def test_a_failed_read_contributes_nothing_rather_than_zero():
    """A refusal is not evidence the patient has none of something."""
    resources = {
        "Condition": normalize.normalize_failure(
            fhir_type="Condition", status_code=503, body=None
        )
    }

    [conditions] = [
        s for s in summary.build_sections([("EPIC", resources)], limit=20)
        if s["key"] == "conditions"
    ]

    assert conditions["total"] == 0
    assert conditions["items"] == []
    # And it is named, so the empty section is visibly unanswered.
    assert summary.collect_issues([("EPIC", resources)]) == [
        {"provider": "EPIC", "type": "Condition", "error": "Provider returned HTTP 503"}
    ]


def test_two_providers_merge_into_one_section_keeping_provenance():
    epic = {"Condition": _envelope("Condition", [_condition("1", "Asthma", "2019-05-02")])}
    cerner = {"Condition": _envelope("Condition", [_condition("2", "Eczema", "2022-08-11")])}

    [conditions] = [
        s for s in summary.build_sections([("EPIC", epic), ("CERNER", cerner)], limit=20)
        if s["key"] == "conditions"
    ]

    assert [item["title"] for item in conditions["items"]] == ["Eczema", "Asthma"]
    assert [item["provider"] for item in conditions["items"]] == ["CERNER", "EPIC"]
    assert conditions["total"] == 2


def test_the_two_observation_searches_stay_in_their_own_sections():
    """Vital signs and laboratory results are both Observations, and a reader
    does not want them in one list."""
    resources = {
        "ObservationVitalSigns": _envelope(
            "Observation",
            [{"resourceType": "Observation", "id": "bp", "status": "final",
              "code": {"text": "Blood pressure"}}],
        ),
        "ObservationLaboratory": _envelope(
            "Observation",
            [{"resourceType": "Observation", "id": "k", "status": "final",
              "code": {"text": "Potassium"}}],
        ),
    }

    sections = {s["key"]: s for s in summary.build_sections([("EPIC", resources)], limit=20)}

    assert [i["title"] for i in sections["vitalSigns"]["items"]] == ["Blood pressure"]
    assert [i["title"] for i in sections["labs"]["items"]] == ["Potassium"]


def test_demographics_come_from_one_provider_and_name_them_all():
    """Two servers will not agree in every detail, and merging them field by
    field would describe a person neither of them did."""
    epic = {"Patient": _envelope("Patient", [_patient("Shaw", "female", "1987-02-20")])}
    cerner = {"Patient": _envelope("Patient", [_patient("Shaw-Reid", "female", "1987-02-21")])}

    demographics = summary.build_demographics([("EPIC", epic), ("CERNER", cerner)])

    assert demographics["name"] == "Shaw"
    assert demographics["birthDate"] == "1987-02-20"
    assert demographics["sources"] == ["EPIC", "CERNER"]


def test_demographics_are_absent_when_no_provider_could_supply_them():
    failed = {
        "Patient": normalize.normalize_failure(
            fhir_type="Patient", status_code=None, body=None
        )
    }

    assert summary.build_demographics([("EPIC", failed)]) is None


def test_every_section_names_a_resource_the_backend_can_actually_fetch():
    """A typo here would leave a section permanently empty, which reads as the
    patient having none of it rather than as the mistake it is."""
    assert summary.SUMMARY_RESOURCES <= set(config.RESOURCE_FETCH_CONFIG)
