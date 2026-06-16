from app.core.config import get_settings

_settings = get_settings()
_redirect_uri = _settings.frontend_hostname.rstrip("/") + "/auth/callback"

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
        "scopes": "launch/patient patient/*.read openid profile offline_access",
        "allowed_issuers": [_settings.epic_issuer] if _settings.epic_issuer else [],
    },
    "EPIC_SANDBOX": {
        "client_id": _settings.epic_sandbox_client_id,
        "client_secret": _settings.epic_sandbox_client_secret,
        "redirect_uri": _redirect_uri,
        "scopes": "launch/patient patient/*.read openid profile offline_access",
        "allowed_issuers": [
            "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
        ],
    },
}




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
    "MedicationOrder": {"url_template": "/MedicationOrder", "needs_patient_param": True},
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
    "ProcedureRequest": {"url_template": "/ProcedureRequest", "needs_patient_param": True},
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
