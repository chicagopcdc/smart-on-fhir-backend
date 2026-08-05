"""A patient's record folded into a clinical summary.

The read path returns one envelope per fetch config row per connection, which is
the right shape for a record: a Condition from Epic and a Condition from Cerner
are different resources on different servers, and merging them there would assert
a claim about the data that nothing supports.

A summary is a view rather than a record, so merging is the point. It answers
"what is going on with this patient" across everything they have connected, in
sections a reader recognises, newest first. Each item keeps the connection it
came from, so a merged view never loses provenance.

Pure functions over the dicts ``app/fhir/normalize.py`` produces. Nothing here
performs I/O, so the shape of a summary can be exercised without a server.
"""

from __future__ import annotations

from typing import Any, Sequence

from app.providers import config

# Which fetch config rows feed which section. Keyed by the row name rather than
# the FHIR type because the Observation searches are split by category, and those
# categories are exactly the distinction a reader cares about: a blood pressure
# and a potassium level are both Observations and belong in different places.
#
# Ordered as a clinician reads a chart: who the patient is, what they have, what
# they are on, what they react to, then the record of what has been done.
SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("conditions", "Conditions", ("Condition",)),
    ("medications", "Medications", ("MedicationRequest",)),
    ("allergies", "Allergies and intolerances", ("AllergyIntolerance",)),
    ("immunizations", "Immunizations", ("Immunization",)),
    ("vitalSigns", "Vital signs", ("ObservationVitalSigns",)),
    ("labs", "Laboratory results", ("ObservationLaboratory",)),
    ("procedures", "Procedures", ("Procedure",)),
    ("encounters", "Encounters", ("Encounter",)),
    ("reports", "Diagnostic reports", ("DiagnosticReport",)),
)

# The fetch config rows a summary needs. Patient is not a section — it is who the
# sections are about — so it is named separately and read for demographics.
DEMOGRAPHICS_SOURCE = "Patient"
SUMMARY_RESOURCES: frozenset[str] = frozenset(
    {DEMOGRAPHICS_SOURCE} | {name for _, _, names in SECTIONS for name in names}
)


def validate_section_sources(names: frozenset[str], configs: dict) -> None:
    """Fail fast if a section names a resource the fetch config cannot supply.

    A name here that matches no fetch config row would leave its section
    permanently empty, which reads as "this patient has none" rather than as the
    typo it is. The same guard as ``US_CORE_RESOURCES`` gets in
    ``app/providers/config.py``, for the same reason.
    """
    unknown = sorted(names - configs.keys())
    if unknown:
        raise ValueError(
            f"summary sections name resources missing from RESOURCE_FETCH_CONFIG: {unknown}"
        )


validate_section_sources(SUMMARY_RESOURCES, config.RESOURCE_FETCH_CONFIG)


def _newest_first(items: list[dict]) -> list[dict]:
    """Order a section newest first, with undated resources last rather than first.

    ISO 8601 strings sort chronologically as text, so a partial date such as
    ``2019-03`` still lands right without parsing. Dated and undated are ordered
    separately because one reversed sort would float the undated to the top,
    pushing real findings out of a capped section.
    """
    dated = [item for item in items if item.get("date")]
    undated = [item for item in items if not item.get("date")]
    dated.sort(key=lambda item: item["date"], reverse=True)
    return dated + undated


def _entries(envelope: Any) -> list[dict]:
    """The usable summaries in one envelope, or nothing if the read failed."""
    if not isinstance(envelope, dict) or envelope.get("status") != "ok":
        return []
    return [entry for entry in envelope.get("entries") or [] if isinstance(entry, dict)]


def _reported_total(envelope: Any, counted: int) -> int:
    """How many the server holds, falling back to how many arrived.

    ``total`` is what the server claims and is absent on the many servers that
    omit ``Bundle.total``. Where it is present it is the more honest number,
    because one page was read and there may be more behind it. A read that
    failed contributes nothing: it is not evidence of zero.
    """
    if not isinstance(envelope, dict) or envelope.get("status") != "ok":
        return 0
    total = envelope.get("total")
    return total if isinstance(total, int) and not isinstance(total, bool) else counted


def build_sections(
    per_connection: Sequence[tuple[str, dict]], *, limit: int
) -> list[dict]:
    """Fold every connection's envelopes into the ordered sections.

    ``per_connection`` is ``(provider, resources)`` pairs, where ``resources`` is
    the mapping the read path returns. Every section is present even when it is
    empty, so a consumer reads a fixed shape rather than probing for keys.
    """
    sections = []

    for key, title, resource_names in SECTIONS:
        items: list[dict] = []
        total = 0

        for provider, resources in per_connection:
            for name in resource_names:
                envelope = (resources or {}).get(name)
                entries = _entries(envelope)
                total += _reported_total(envelope, len(entries))
                # Provenance travels with the item, so a merged section can still
                # answer which server each finding came from.
                items.extend({**entry, "provider": provider} for entry in entries)

        items = _newest_first(items)
        sections.append(
            {
                "key": key,
                "title": title,
                "resourceTypes": list(resource_names),
                "total": total,
                "returned": min(len(items), limit),
                "items": items[:limit],
            }
        )

    return sections


def build_demographics(per_connection: Sequence[tuple[str, dict]]) -> dict | None:
    """Who the patient is, from whichever connections could say.

    A record can span providers that each hold their own Patient resource, and
    they will not agree in every detail. The first one that parsed is used rather
    than merging them field by field, which would produce a person no server
    actually described; ``sources`` names every connection that supplied one so a
    consumer can go and compare if it matters to them.
    """
    described: dict | None = None
    sources: list[str] = []

    for provider, resources in per_connection:
        entries = _entries((resources or {}).get(DEMOGRAPHICS_SOURCE))
        if not entries:
            continue
        sources.append(provider)
        if described is None:
            described = entries[0]

    if described is None:
        return None

    detail = described.get("detail") or {}
    return {
        "name": described.get("title"),
        "gender": detail.get("gender"),
        "birthDate": detail.get("birthDate"),
        "deceased": detail.get("deceased"),
        "sources": sources,
    }


def collect_issues(per_connection: Sequence[tuple[str, dict]]) -> list[dict]:
    """Every resource read that failed, named by connection and type.

    A summary that quietly leaves out what it could not read looks the same as
    one for a patient who has none of it. These are what make a partial answer
    visibly partial.
    """
    issues = []
    for provider, resources in per_connection:
        for name, envelope in (resources or {}).items():
            if isinstance(envelope, dict) and envelope.get("status") != "ok":
                issues.append(
                    {
                        "provider": provider,
                        "type": name,
                        "error": envelope.get("error") or "The read failed",
                    }
                )
    return issues
