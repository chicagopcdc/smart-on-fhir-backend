"""Turn what a provider sent into one shape the rest of the app can rely on.

Two servers, asked for the same patient's conditions, answer with the same
clinical facts arranged differently: one puts the label in ``code.text``, the
next only in ``code.coding[0].display``; one dates the condition with
``onsetDateTime``, the next with ``recordedDate``; a search may or may not carry
a ``total``. Handing that straight to a caller makes every consumer re-learn each
vendor's habits.

So every response becomes the same envelope, and every resource inside it the
same summary, with the validated resource travelling alongside so nothing is
lost. Parsing goes through the ``fhir.resources`` R4B models, which is also what
catches a resource that is not really the type it claims to be.

Deliberately **not** ``Bundle.model_validate(payload)``: a bundle entry's
``resource`` is polymorphic, so one bad resource would fail the whole bundle and
cost a patient their entire record over a single unparseable row. Entries are
read from the envelope and validated one at a time instead, and a resource that
will not parse is dropped with a warning while the rest come through.

US Core, the profile set a certified server must support, is what the summaries
are written against: https://hl7.org/fhir/us/core/
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from typing import Any, Callable

from fhir.resources.R4B import get_fhir_model_class
from pydantic import ValidationError

logger = logging.getLogger(__name__)


def normalize_response(payload: Any, *, fhir_type: str, status_code: int = 200) -> dict:
    """Normalize one provider response into the common envelope.

    ``payload`` is whatever the server's body deserialized to: a searchset
    Bundle, a bare resource (``Patient`` is read as ``/Patient/{id}``, so it
    arrives on its own), or an OperationOutcome describing a refusal.

    ``fhir_type`` is the type that was asked for. It is what names an empty
    result, which carries no resource of its own to read a type from.
    """
    if not isinstance(payload, dict):
        return failed_response(
            fhir_type=fhir_type,
            error="Provider returned a body that is not a FHIR resource",
            status_code=status_code,
        )

    kind = payload.get("resourceType")

    # A server may answer 200 with an OperationOutcome. Reading that as "no
    # resources found" would show an empty record where there was a fault.
    if kind == "OperationOutcome":
        return failed_response(
            fhir_type=fhir_type,
            error=_outcome_text(payload),
            status_code=status_code,
        )

    if kind == "Bundle":
        resources = []
        skipped = 0
        for entry in payload.get("entry") or []:
            resource = entry.get("resource") if isinstance(entry, dict) else None
            if not isinstance(resource, dict):
                # An entry can carry a search mode or a request and no resource.
                skipped += 1
            elif resource.get("resourceType") == fhir_type:
                resources.append(resource)
            # Anything else is a warning about the search itself (an
            # OperationOutcome entry) or a resource pulled in beside the
            # matches. Neither was asked for, so neither belongs in the record,
            # and neither is a failure, so neither counts against skipped.
        # Bundle.total is optional and plenty of servers omit it, so "there is
        # more" has to be read from the paging link, which is always there.
        # bool is an int in Python, hence the second check.
        total = payload.get("total")
        if not isinstance(total, int) or isinstance(total, bool):
            total = None
        truncated = any(
            link.get("relation") == "next"
            for link in payload.get("link") or []
            if isinstance(link, dict)
        )
    elif kind == fhir_type:
        resources, skipped, total, truncated = [payload], 0, 1, False
    else:
        # A read of a single resource that came back as something else returned
        # nothing that was asked for, so unlike a foreign entry in a bundle this
        # is a failed read rather than a resource to leave out. It also covers a
        # 200 whose body is a gateway's own JSON and names no resource at all.
        return failed_response(
            fhir_type=fhir_type,
            error=f"Provider returned {kind or 'a body with no resource type'} "
            f"where {fhir_type} was expected",
            status_code=status_code,
        )

    entries = []
    for resource in resources:
        summary = _summarize(resource)
        if summary is None:
            skipped += 1
        else:
            entries.append(summary)

    return _envelope(
        resource_type=fhir_type,
        status="ok",
        status_code=status_code,
        entries=entries,
        total=total,
        truncated=truncated,
        skipped=skipped,
    )


def failed_response(*, fhir_type: str, error: str, status_code: int | None) -> dict:
    """The same envelope for a response that never produced resources.

    A caller should not have to branch on shape to find out a read failed, so a
    refusal, a timeout and an empty record all arrive with the same keys.
    """
    return _envelope(
        resource_type=fhir_type,
        status="error",
        status_code=status_code,
        entries=[],
        total=None,
        truncated=False,
        skipped=0,
        error=error,
    )


def normalize_failure(*, fhir_type: str, status_code: int | None, body: str | None) -> dict:
    """The failure envelope for a fetch that never returned a resource.

    Turns a raw HTTP status and body into a reason and wraps it, so a caller need
    not compose failure_message with failed_response itself.
    """
    return failed_response(
        fhir_type=fhir_type,
        error=failure_message(status_code, body),
        status_code=status_code,
    )


def failure_message(status_code: int | None, body: str | None) -> str:
    """A short reason for a fetch that never returned a resource.

    A provider's error body is an OperationOutcome when it is FHIR at all and an
    HTML error page when it is not. Only the first says anything useful, and
    passing the second along would put a vendor's internals in our response, so
    anything else becomes the status code alone.
    """
    if body:
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("resourceType") == "OperationOutcome":
            return _outcome_text(parsed)
    if status_code is None:
        return "Could not reach the provider"
    return f"Provider returned HTTP {status_code}"


def _envelope(
    *,
    resource_type: str,
    status: str,
    status_code: int | None,
    entries: list[dict],
    total: int | None,
    truncated: bool,
    skipped: int,
    error: str | None = None,
) -> dict:
    return {
        "resourceType": resource_type,
        "status": status,
        "statusCode": status_code,
        "count": len(entries),
        # What the server says it holds, against what this page returned. Absent
        # on servers that omit Bundle.total.
        "total": total,
        "truncated": truncated,
        # Entries that yielded no usable resource (unparseable, or nothing to
        # parse). Surfaced so a partial record is visibly partial.
        "skipped": skipped,
        "error": error,
        "entries": entries,
    }


def _outcome_text(outcome: dict) -> str:
    """The readable part of an OperationOutcome, for the envelope's error."""
    messages = []
    for issue in outcome.get("issue") or []:
        if not isinstance(issue, dict):
            continue
        text = issue.get("diagnostics") or (issue.get("details") or {}).get("text")
        messages.append(text or issue.get("code") or "unknown issue")
    return "; ".join(messages) or "Provider returned an OperationOutcome"


def _summarize(raw: dict) -> dict | None:
    """One resource as a summary, or None if it could not be parsed."""
    resource_type = raw.get("resourceType")
    # get_fhir_model_class builds an attribute name from this, so anything other
    # than a string raises a TypeError that is not a validation failure. Checked
    # here rather than relied on from the caller, so this stays safe on any input.
    if not isinstance(resource_type, str):
        _log_skipped(None, raw.get("id"), "resourceType is missing or not a string")
        return None

    try:
        model = get_fhir_model_class(resource_type).model_validate(raw)
    except ValidationError as exc:
        _log_skipped(resource_type, raw.get("id"), _problems(exc))
        return None
    except ValueError as exc:
        # get_fhir_model_class rejects a type it does not know.
        _log_skipped(resource_type, raw.get("id"), str(exc))
        return None

    summarize = _SUMMARIZERS.get(resource_type, _summarize_any)
    try:
        summary = summarize(model)
    except Exception as exc:  # noqa: BLE001
        # One resource must never cost the whole read, so a summarizer that trips
        # over an unforeseen but valid shape is dropped like a parse failure.
        # Log only the exception type: its message could quote a clinical value.
        _log_skipped(resource_type, raw.get("id"), f"summary failed ({type(exc).__name__})")
        return None

    return {
        "id": model.id,
        "resourceType": model.get_resource_type(),
        "title": summary.get("title"),
        "code": summary.get("code"),
        "status": summary.get("status"),
        "date": summary.get("date"),
        "category": summary.get("category"),
        "detail": summary.get("detail") or {},
        # The body as the server sent it. The models are extra="forbid", so a
        # re-serialized model would carry the same fields, at the cost of extra
        # work and the server's original timestamps.
        "resource": raw,
    }


def _problems(exc: ValidationError) -> str:
    """A validation failure described by field and reason only.

    A ValidationError renders the offending input in ``str(exc)``, so logging the
    exception would put clinical values in the logs. ``loc`` names the field and
    ``msg`` says what was wrong with it; neither carries the value.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )


def _log_skipped(resource_type: str | None, resource_id: str | None, problems: str) -> None:
    logger.warning(
        "Skipped an unparseable %s resource (id=%s): %s",
        resource_type or "unknown",
        resource_id or "unknown",
        problems,
    )


# --- reading the pieces FHIR spells several ways -----------------------------


def _first(values):
    return values[0] if values else None


def _concept_text(concept) -> str | None:
    """The human label of a CodeableConcept.

    ``text`` is the server's own wording and wins where present; a coding's
    ``display`` is the fallback, and a bare code is better than nothing.
    """
    if concept is None:
        return None
    if getattr(concept, "text", None):
        return concept.text
    codings = getattr(concept, "coding", None) or []
    for coding in codings:
        if coding.display:
            return coding.display
    for coding in codings:
        if coding.code:
            return coding.code
    return None


def _concept_coding(concept) -> dict | None:
    """The first coding of a CodeableConcept, as the terminology triple."""
    coding = _first(getattr(concept, "coding", None) or [])
    if coding is None:
        return None
    return {
        "system": str(coding.system) if coding.system else None,
        "code": coding.code,
        "display": coding.display,
    }


def _concept_code(concept) -> str | None:
    coding = _first(getattr(concept, "coding", None) or [])
    return coding.code if coding else None


def _category_code(value) -> str | None:
    """A category as a plain code, whichever way the resource spells it.

    Most types carry a list of CodeableConcepts, Procedure carries one, and
    AllergyIntolerance carries bare codes.
    """
    if isinstance(value, list):
        value = _first(value)
    if value is None or isinstance(value, str):
        return value
    return _concept_code(value)


def _iso(value) -> str | None:
    """A FHIR date, dateTime or instant as an ISO 8601 string."""
    if value is None:
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def _first_date(*values) -> str | None:
    """The first of several date fields that the resource actually filled in."""
    for value in values:
        rendered = _iso(value)
        if rendered:
            return rendered
    return None


def _period_start(period):
    return getattr(period, "start", None)


def _number(value) -> float | None:
    """A FHIR decimal as a JSON number.

    The models parse decimals as ``Decimal``, which json cannot serialize.
    """
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal) else value


def _human_name(names) -> str | None:
    """A patient's name, preferring the one the server marked official."""
    if not names:
        return None
    name = next((n for n in names if n.use == "official"), names[0])
    if name.text:
        return name.text
    parts = list(name.given or []) + ([name.family] if name.family else [])
    return " ".join(parts) or None


# --- one summary per US Core type --------------------------------------------
#
# Each reads the fields that matter for its type into the common slots. The
# judgment is in which field feeds which slot: a Condition's onset is when the
# patient got it, while recordedDate is only when someone typed it in, so onset
# wins where both are present.


def _summarize_patient(r) -> dict:
    deceased = r.deceasedBoolean
    if deceased is None and r.deceasedDateTime is not None:
        deceased = True
    return {
        "title": _human_name(r.name),
        "status": None if r.active is None else ("active" if r.active else "inactive"),
        "date": _iso(r.birthDate),
        "detail": {
            "gender": r.gender,
            "birthDate": _iso(r.birthDate),
            "deceased": deceased,
        },
    }


def _summarize_condition(r) -> dict:
    return {
        "title": _concept_text(r.code),
        "code": _concept_coding(r.code),
        "status": _concept_code(r.clinicalStatus),
        "date": _first_date(r.onsetDateTime, _period_start(r.onsetPeriod), r.recordedDate),
        "category": _category_code(r.category),
        "detail": {
            "verificationStatus": _concept_code(r.verificationStatus),
            "severity": _concept_text(r.severity),
            "abatement": _first_date(r.abatementDateTime, _period_start(r.abatementPeriod)),
        },
    }


def _summarize_medication_request(r) -> dict:
    concept = r.medicationCodeableConcept
    reference = r.medicationReference
    dosage = _first(r.dosageInstruction or [])
    return {
        # A medication is named inline or by reference; the reference's display
        # is the only label available without a second fetch.
        "title": _concept_text(concept) or (reference.display if reference else None),
        "code": _concept_coding(concept),
        "status": r.status,
        "date": _iso(r.authoredOn),
        "category": _category_code(r.category),
        "detail": {
            "intent": r.intent,
            "dosage": dosage.text if dosage else None,
        },
    }


def _summarize_allergy_intolerance(r) -> dict:
    return {
        "title": _concept_text(r.code),
        "code": _concept_coding(r.code),
        "status": _concept_code(r.clinicalStatus),
        "date": _first_date(r.onsetDateTime, r.recordedDate),
        "category": _category_code(r.category),
        "detail": {
            "criticality": r.criticality,
            "type": r.type,
            "reactions": [
                _concept_text(manifestation)
                for reaction in r.reaction or []
                for manifestation in reaction.manifestation or []
            ],
        },
    }


def _summarize_immunization(r) -> dict:
    return {
        "title": _concept_text(r.vaccineCode),
        "code": _concept_coding(r.vaccineCode),
        "status": r.status,
        "date": _iso(r.occurrenceDateTime),
        "detail": {
            "lotNumber": r.lotNumber,
            "primarySource": r.primarySource,
        },
    }


def _summarize_procedure(r) -> dict:
    return {
        "title": _concept_text(r.code),
        "code": _concept_coding(r.code),
        "status": r.status,
        "date": _first_date(r.performedDateTime, _period_start(r.performedPeriod)),
        "category": _category_code(r.category),
        "detail": {"outcome": _concept_text(r.outcome)},
    }


def _summarize_encounter(r) -> dict:
    kind = _first(r.type or [])
    return {
        # Encounter.class is a Coding, and the models spell it class_fhir since
        # class is a Python keyword.
        "title": _concept_text(kind) or (r.class_fhir.display if r.class_fhir else None),
        "code": _concept_coding(kind),
        "status": r.status,
        "date": _iso(_period_start(r.period)),
        "category": r.class_fhir.code if r.class_fhir else None,
        "detail": {
            "end": _iso(getattr(r.period, "end", None)),
            "serviceType": _concept_text(r.serviceType),
        },
    }


def _summarize_diagnostic_report(r) -> dict:
    return {
        "title": _concept_text(r.code),
        "code": _concept_coding(r.code),
        "status": r.status,
        "date": _first_date(r.effectiveDateTime, _period_start(r.effectivePeriod), r.issued),
        "category": _category_code(r.category),
        "detail": {
            "conclusion": r.conclusion,
            # The observations behind the report. They are references, and the
            # default read fetches lab Observations too, so a caller can join.
            "resultCount": len(r.result or []),
        },
    }


def _scalar_value(node) -> tuple[Any, str | None]:
    """The result an Observation or one of its components carries, as (value, unit).

    ``value[x]`` is a choice type: the same slot holds a measured number, a coded
    answer or plain text depending on what was observed. Components offer exactly
    the same choice, which is why this reads either.

    Choices that are not a single scalar (a range, a ratio, sampled waveform
    data) stay ``None`` here rather than being flattened; they remain in full on
    the resource.
    """
    quantity = getattr(node, "valueQuantity", None)
    if quantity is not None:
        # unit is the human wording and is optional; code is the UCUM symbol.
        return _number(quantity.value), quantity.unit or quantity.code

    concept = getattr(node, "valueCodeableConcept", None)
    if concept is not None:
        return _concept_text(concept), None

    for field in ("valueString", "valueBoolean", "valueInteger"):
        value = getattr(node, field, None)
        if value is not None:
            return value, None

    moment = getattr(node, "valueDateTime", None)
    if moment is not None:
        return _iso(moment), None

    return None, None


def _summarize_component(component) -> dict:
    """One measurement inside a panel, carrying the code that identifies it."""
    value, unit = _scalar_value(component)
    return {
        "title": _concept_text(component.code),
        "code": _concept_coding(component.code),
        "value": value,
        "unit": unit,
    }


def _reference_range(ranges) -> dict | None:
    """The normal range for a result, kept as bounds rather than rendered.

    Worth carrying: in pediatrics the range moves with age, so a value alone is
    not interpretable. Kept structured so a consumer flagging out-of-range
    results need not parse a sentence; the server's wording rides in ``text``.
    """
    reference = _first(ranges or [])
    if reference is None:
        return None
    return {
        "low": _number(reference.low.value) if reference.low else None,
        "high": _number(reference.high.value) if reference.high else None,
        "unit": (reference.low.unit if reference.low else None)
        or (reference.high.unit if reference.high else None),
        "text": reference.text,
    }


def _summarize_observation(r) -> dict:
    """An Observation: what was measured, and what came back.

    A panel such as a blood pressure carries no value of its own; the result
    lives only in its components. So a panel's ``value`` stays None and the
    readings travel in ``components`` rather than being joined into a display
    string like "108/72".
    """
    value, unit = _scalar_value(r)
    return {
        "title": _concept_text(r.code),
        "code": _concept_coding(r.code),
        "status": r.status,
        # effective is when the measurement applies to the patient; issued is
        # only when the lab released it, so it is the fallback.
        "date": _first_date(r.effectiveDateTime, _period_start(r.effectivePeriod), r.issued),
        "category": _category_code(r.category),
        "detail": {
            "value": value,
            "unit": unit,
            "components": [_summarize_component(c) for c in r.component or []],
            "interpretation": _concept_text(_first(r.interpretation or [])),
            "referenceRange": _reference_range(r.referenceRange),
        },
    }


def _summarize_any(r) -> dict:
    """The fallback for a type with no summary of its own.

    ``?include=all`` reaches the long tail, and those still have to arrive in the
    same shape. Only fields that mean the same thing across resources are read,
    so the result is thin rather than wrong.
    """
    code = getattr(r, "code", None)
    # Some resources call something else "code" entirely, so only read it when it
    # is a CodeableConcept.
    if not hasattr(code, "coding"):
        code = None
    return {
        "title": _concept_text(code),
        "code": _concept_coding(code),
        "status": getattr(r, "status", None),
        "date": _first_date(
            getattr(r, "effectiveDateTime", None),
            getattr(r, "authoredOn", None),
            getattr(r, "date", None),
            getattr(r, "created", None),
            _period_start(getattr(r, "period", None)),
        ),
    }


_SUMMARIZERS: dict[str, Callable[[Any], dict]] = {
    "AllergyIntolerance": _summarize_allergy_intolerance,
    "Condition": _summarize_condition,
    "DiagnosticReport": _summarize_diagnostic_report,
    "Encounter": _summarize_encounter,
    "Immunization": _summarize_immunization,
    "MedicationRequest": _summarize_medication_request,
    "Observation": _summarize_observation,
    "Patient": _summarize_patient,
    "Procedure": _summarize_procedure,
}


def warm_model_cache() -> None:
    """Build the pydantic model classes a default read needs, up front.

    get_fhir_model_class builds each class lazily on first use, which would
    otherwise land ~100ms on the event loop during the first read. The summarized
    types are the ones the US Core tier touches.
    """
    for fhir_type in _SUMMARIZERS:
        get_fhir_model_class(fhir_type)
