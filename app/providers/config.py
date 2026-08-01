from enum import Enum

from app.core.config import get_settings

_settings = get_settings()
_redirect_uri = _settings.frontend_hostname.rstrip("/") + "/auth/callback"

_DEFAULT_SCOPES = "launch/patient patient/*.read openid profile offline_access"

# Per-provider registration data that a SMART server does NOT advertise:
# the client credentials, where the EHR redirects back to, and the scopes we
# request. Endpoints and capabilities are discovered at runtime instead.
#
# "allowed_issuers" pins which FHIR base URLs a caller may start an
# authorization against. A confidential client must never send its secret to an
# issuer it did not register with, so the supplied iss is checked against this
# allowlist before any discovery request is made.
EHR_CONFIGS = {
    "EPIC": {
        "client_id": _settings.epic_client_id,
        "client_secret": _settings.epic_client_secret,
        "redirect_uri": _redirect_uri,
        "scopes": _DEFAULT_SCOPES,
        "allowed_issuers": [_settings.epic_issuer] if _settings.epic_issuer else [],
    },
    "EPIC_SANDBOX": {
        "client_id": _settings.epic_sandbox_client_id,
        "client_secret": _settings.epic_sandbox_client_secret,
        "redirect_uri": _redirect_uri,
        "scopes": _DEFAULT_SCOPES,
        "allowed_issuers": [
            "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
        ],
    },
    # Public SMART App Launcher. A public client (no secret) that relies on PKCE;
    # the launcher does not validate the client_id, so a default keeps the flow
    # runnable without any registration.
    "SMART_LAUNCHER": {
        "client_id": _settings.smart_launcher_client_id or "smart-fhir-backend",
        "client_secret": None,
        "redirect_uri": _redirect_uri,
        "scopes": _DEFAULT_SCOPES,
        # The launcher carries a standalone launch's context in the aud path
        # (.../v/r4/sim/<base64 options>/fhir) and refuses one made against the
        # plain FHIR base with "Invalid launch options". So the issuer offered
        # here — which is what /providers advertises and a caller connects with —
        # is the sim form, with an empty options object (base64 "{}"). Empty is
        # enough: it satisfies the launcher's parse and leaves it to prompt for
        # the patient, which is what a standalone launch is supposed to do.
        "allowed_issuers": [
            "https://launch.smarthealthit.org/v/r4/sim/e30/fhir",
        ],
        # Accept any FHIR base under the launcher's r4 tree, so a caller that
        # builds its own sim options, or uses the plain base for an EHR launch,
        # still authorizes. Safe as a prefix only here: this is a public client
        # (no secret is ever sent), the prefix is same-host, and it ends in
        # "/v/r4/" so a suffix look-alike host such as
        # "launch.smarthealthit.org.evil.example/..." cannot match. Real EHRs
        # keep exact-match allowlisting (no prefixes) for tenant isolation.
        "allowed_issuer_prefixes": [
            "https://launch.smarthealthit.org/v/r4/",
        ],
    },
    # Cerner / Oracle Health sandbox, registered as a public client. Discovery
    # advertises S256, so the same adapter turns on PKCE automatically.
    "CERNER_SANDBOX": {
        "client_id": _settings.cerner_client_id,
        "client_secret": None,
        "redirect_uri": _redirect_uri,
        "scopes": _DEFAULT_SCOPES,
        "allowed_issuers": [
            "https://fhir-ehr-code.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d",
        ],
    },
}


def validate_issuer_prefixes(configs: dict) -> None:
    """Fail fast if any provider's ``allowed_issuer_prefixes`` is unsafe.

    Prefix matching in ``start_auth`` widens which issuers a provider may talk to,
    so it is only safe when two invariants hold — both enforced here rather than
    left to a comment, so they cannot silently rot as configs change:

    * The provider sends **no client secret**. Prefix matching on a confidential
      client would let its secret reach any host under the prefix.
    * Each prefix is an ``https`` URL whose authority is terminated by a path
      separator, so a suffix look-alike host (``…smarthealthit.org.evil.example/…``)
      cannot match it. (A bare string used in place of the list would otherwise be
      iterated character by character.)
    """
    for key, ehr in configs.items():
        prefixes = ehr.get("allowed_issuer_prefixes", [])
        if not isinstance(prefixes, list):
            raise ValueError(
                f"allowed_issuer_prefixes for {key!r} must be a list, got {type(prefixes).__name__}"
            )
        if prefixes and ehr.get("client_secret") is not None:
            raise ValueError(
                f"{key!r} sets allowed_issuer_prefixes but has a client_secret; "
                "prefix matching is only safe for a public client that sends no secret"
            )
        for prefix in prefixes:
            if (
                not isinstance(prefix, str)
                or not prefix.startswith("https://")
                or "/" not in prefix[len("https://") :]
            ):
                raise ValueError(
                    f"unsafe allowed_issuer_prefixes entry for {key!r}: {prefix!r} — "
                    "must be an https URL with a path so the host is fully committed"
                )


# Validate at import so a misconfigured prefix fails at startup, not at runtime.
validate_issuer_prefixes(EHR_CONFIGS)


def configured_providers() -> list[dict]:
    """The providers this backend can actually authorize against, for the frontend's
    dropdown: one ``{"provider", "iss", "name"}`` entry per allowed issuer.

    Providers with no ``client_id`` are skipped — ``/auth/connect`` rejects those as
    unconfigured, so offering them would be a dead end. Derived from ``EHR_CONFIGS`` so
    the list never drifts from what we can connect to, and grows automatically as
    providers are added (nothing to keep in sync on the frontend). An issuer is listed
    once even if two providers allow it, since the frontend keys its options by issuer.
    """
    by_issuer: dict[str, dict] = {}
    for key, ehr in EHR_CONFIGS.items():
        if not ehr.get("client_id"):
            continue
        for iss in ehr.get("allowed_issuers", []):
            if iss:
                # Canonicalize the same way /auth/connect does, so the issuer offered here
                # is exactly the one the allowlist accepts back.
                iss = iss.rstrip("/")
                by_issuer.setdefault(
                    iss,
                    {"provider": key, "iss": iss, "name": key.replace("_", " ").title()},
                )
    return list(by_issuer.values())


RESOURCE_FETCH_CONFIG = {
    "Account": {"url_template": "/Account", "needs_patient_param": True},
    "AdverseEvent": {"url_template": "/AdverseEvent", "needs_patient_param": True},
    "AllergyIntolerance": {"url_template": "/AllergyIntolerance", "needs_patient_param": True},
    "Appointment": {"url_template": "/Appointment", "needs_patient_param": True},
    "Binary": {"url_template": "/Binary/{patient_id}", "needs_patient_param": False},
    "BodyStructure": {"url_template": "/BodyStructure", "needs_patient_param": True},
    "CarePlan": {"url_template": "/CarePlan", "needs_patient_param": True},
    "CareTeam": {"url_template": "/CareTeam", "needs_patient_param": True},
    "Communication": {"url_template": "/Communication", "needs_patient_param": True},
    "Condition": {"url_template": "/Condition", "needs_patient_param": True},
    "Consent": {"url_template": "/Consent", "needs_patient_param": True},
    "Coverage": {"url_template": "/Coverage", "needs_patient_param": True},
    "Device": {"url_template": "/Device", "needs_patient_param": True},
    "DeviceRequest": {"url_template": "/DeviceRequest", "needs_patient_param": True},
    "DeviceUseStatement": {"url_template": "/DeviceUseStatement", "needs_patient_param": True},
    "DiagnosticReport": {"url_template": "/DiagnosticReport", "needs_patient_param": True},
    "DocumentReference": {"url_template": "/DocumentReference", "needs_patient_param": True},
    "Encounter": {"url_template": "/Encounter", "needs_patient_param": True},
    "EpisodeOfCare": {"url_template": "/EpisodeOfCare", "needs_patient_param": True},
    "ExplanationOfBenefit": {"url_template": "/ExplanationOfBenefit", "needs_patient_param": True},
    "FamilyMemberHistory": {"url_template": "/FamilyMemberHistory", "needs_patient_param": True},
    "Flag": {"url_template": "/Flag", "needs_patient_param": True},
    "Goal": {"url_template": "/Goal", "needs_patient_param": True},
    "Immunization": {"url_template": "/Immunization", "needs_patient_param": True},
    "ImmunizationRecommendation": {"url_template": "/ImmunizationRecommendation", "needs_patient_param": True},
    "List": {"url_template": "/List", "needs_patient_param": True},
    "Location": {"url_template": "/Location", "needs_patient_param": True},
    "Media": {"url_template": "/Media", "needs_patient_param": True},
    "Medication": {"url_template": "/Medication", "needs_patient_param": True},
    "MedicationDispense": {"url_template": "/MedicationDispense", "needs_patient_param": True},
    "MedicationRequest": {"url_template": "/MedicationRequest", "needs_patient_param": True},
    "MedicationStatement": {"url_template": "/MedicationStatement", "needs_patient_param": True},
    "NutritionOrder": {"url_template": "/NutritionOrder", "needs_patient_param": True},
    # "Observation": {"url_template": "/Observation", "needs_patient_param": True, "extra_params": {"category": "laboratory"}},
    "ObservationVitalSigns": {"url_template": "/Observation", "needs_patient_param": True, "extra_params": {"category": "vital-signs"}},
    "ObservationLaboratory": {"url_template": "/Observation", "needs_patient_param": True, "extra_params": {"category": "laboratory"}},
    "ObservationSocialHistory": {"url_template": "/Observation", "needs_patient_param": True, "extra_params": {"category": "social-history"}},
    "ObservationSurvey": {"url_template": "/Observation", "needs_patient_param": True, "extra_params": {"category": "survey"}},
    "ObservationProcedure": {"url_template": "/Observation", "needs_patient_param": True, "extra_params": {"category": "procedure"}},
    "ObservationExam": {"url_template": "/Observation", "needs_patient_param": True, "extra_params": {"category": "exam"}},
    "ObservationTherapy": {"url_template": "/Observation", "needs_patient_param": True, "extra_params": {"category": "therapy"}},
    "ObservationImaging": {"url_template": "/Observation", "needs_patient_param": True, "extra_params": {"category": "imaging"}},
    "Organization": {"url_template": "/Organization", "needs_patient_param": False},
    "Patient": {"url_template": "/Patient/{patient_id}", "needs_patient_param": False},
    "Practitioner": {"url_template": "/Practitioner", "needs_patient_param": False},
    "PractitionerRole": {"url_template": "/PractitionerRole", "needs_patient_param": False},
    "Procedure": {"url_template": "/Procedure", "needs_patient_param": True},
    "Provenance": {"url_template": "/Provenance", "needs_patient_param": True},
    "Questionnaire": {"url_template": "/Questionnaire", "needs_patient_param": False},
    "QuestionnaireResponse": {"url_template": "/QuestionnaireResponse", "needs_patient_param": True},
    "RelatedPerson": {"url_template": "/RelatedPerson", "needs_patient_param": True},
    "RequestGroup": {"url_template": "/RequestGroup", "needs_patient_param": True},
    "ResearchStudy": {"url_template": "/ResearchStudy", "needs_patient_param": False},
    "Schedule": {"url_template": "/Schedule", "needs_patient_param": False},
    "ServiceRequest": {"url_template": "/ServiceRequest", "needs_patient_param": True},
    "Slot": {"url_template": "/Slot", "needs_patient_param": False},
    "Specimen": {"url_template": "/Specimen", "needs_patient_param": True},
    "Substance": {"url_template": "/Substance", "needs_patient_param": False},
    "Task": {"url_template": "/Task", "needs_patient_param": True},
    "ValueSet": {"url_template": "/ValueSet", "needs_patient_param": False},
}


# The resources a read fetches by default. Everything else in
# RESOURCE_FETCH_CONFIG stays reachable through ?include=all.
#
# Since 1 January 2026 an EHR certified under ONC's §170.315(g)(10) criterion
# must expose USCDI v3 through FHIR US Core 6.1.0, so the US Core "must support"
# set is the data any certified server is obliged to return. That makes it the
# right default, but it is still 30-odd profiles, so a name earns a place here
# only if all three hold:
#
#   1. It is a USCDI v3 data class, so every certified server will have it.
#   2. It is scoped to the authorized patient, so the search is bounded. (A bare
#      /Practitioner or /Organization asks the server for its whole directory.)
#   3. It is useful on its own — its clinical content is inline rather than
#      behind a second fetch.
#
# Encounter qualifies and is included: it dates and gives context to every other
# resource, which is what a longitudinal reader needs. DocumentReference fails
# (3) — a server returns note metadata with a Binary link, so without a Binary
# retrieval path it is a pointer we cannot resolve.
US_CORE_RESOURCES = frozenset(
    {
        "Patient",
        "AllergyIntolerance",
        "Condition",
        "DiagnosticReport",
        "Encounter",
        "Immunization",
        "MedicationRequest",
        "ObservationVitalSigns",
        "ObservationLaboratory",
        "Procedure",
    }
)


def validate_us_core_membership(names: frozenset[str], configs: dict) -> None:
    """Fail fast if the default tier names a resource the fetch config does not have.

    Every name in ``US_CORE_RESOURCES`` selects a row from ``RESOURCE_FETCH_CONFIG``.
    A typo would match nothing and silently drop that resource from every default
    read, a gap that surfaces only as missing data much later, so it is caught at
    import instead.
    """
    unknown = sorted(names - configs.keys())
    if unknown:
        raise ValueError(
            f"US_CORE_RESOURCES names resources missing from RESOURCE_FETCH_CONFIG: {unknown}"
        )


validate_us_core_membership(US_CORE_RESOURCES, RESOURCE_FETCH_CONFIG)


# The fetch config's row names as an enum, so a caller naming a resource type
# that does not exist gets a 422 listing what does, rather than a silently empty
# result. Derived from the config so the two cannot drift.
ResourceName = Enum(
    "ResourceName",
    {name: name for name in RESOURCE_FETCH_CONFIG},
    type=str,
    module=__name__,
)
ResourceName.__doc__ = "A resource type a read can ask for."


class ResourceTier(str, Enum):
    """How much of a patient's record a read should cover.

    Subclassing ``str`` (rather than ``StrEnum``, which needs 3.11) keeps the
    package's declared 3.10 floor and lets FastAPI validate the query parameter
    against the members, so an unknown tier is a 422 rather than a silent
    fallback to the default.
    """

    US_CORE = "us-core"
    ALL = "all"


def fhir_type_for(entry: dict) -> str:
    """The FHIR resource type a fetch config row targets.

    Read from the URL rather than declared alongside it, so the two cannot drift.
    It is what names a result that came back empty, and it is not always the row's
    key: the Observation searches are split by category, so ``ObservationLaboratory``
    fetches ``Observation``.
    """
    return entry["url_template"].strip("/").split("/", 1)[0]


def _select(wanted) -> dict:
    """The named fetch config rows, in the order the config declares them.

    Iterating the config rather than the request keeps a read's shape stable: the
    same resources come back in the same order however the caller listed them,
    and a name that matches nothing simply is not there.
    """
    wanted = set(wanted)
    return {name: entry for name, entry in RESOURCE_FETCH_CONFIG.items() if name in wanted}


def resources_for(tier: ResourceTier) -> dict:
    """The fetch config rows a read should cover for the requested tier."""
    return _select(
        RESOURCE_FETCH_CONFIG.keys() if tier is ResourceTier.ALL else US_CORE_RESOURCES
    )


def resources_named(names) -> dict:
    """The fetch config rows for an explicitly named set of resources."""
    return _select(names)
