"""Request and response models for the API.

These are what FastAPI reads to generate the OpenAPI document, so they are the
public contract: a field that is not here is not documented, and a route that
returns something a model does not describe fails loudly rather than shipping an
undocumented shape.

Field names are camelCase, matching FHIR itself and the envelope
``app/fhir/normalize.py`` already produces. ``populate_by_name`` means the models
still accept the Python spelling, so a route can build one from keyword
arguments and let serialization apply the alias.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    """Base for every model in the API: camelCase out, either spelling in."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ErrorResponse(BaseModel):
    """The body of a failed request.

    FastAPI's ``HTTPException`` renders as ``{"detail": ...}``, so declaring it
    here documents what a caller already receives rather than inventing a second
    error shape for them to handle.
    """

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"detail": "Invalid or expired session"}]}
    )

    detail: str


def refusal(description: str) -> dict:
    """One entry for a route's ``responses``, documenting why it was refused.

    Every refusal this API can answer has the same body, so the only thing that
    varies per status is the prose. Building the entry here keeps that true:
    routes name their reasons and never restate the shape.
    """
    return {"model": ErrorResponse, "description": description}


# --- authorization -----------------------------------------------------------


class ConnectRequest(APIModel):
    """Which server to authorize against."""

    provider: str = Field(
        description="The provider registration to authorize with, as listed by "
        "`GET /providers`. It selects the client credentials and the issuers "
        "that are allowed.",
        examples=["EPIC_SANDBOX"],
    )
    iss: str = Field(
        description="The FHIR base URL to connect to. It must be one the "
        "provider is registered for; anything else is refused before any "
        "request is made to it.",
        examples=["https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"],
    )


class ConnectResponse(APIModel):
    """Where to send the patient to authorize, and how long that offer stands."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "authorizationUrl": "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize?response_type=code&client_id=…&state=Yl8k…",
                    "state": "Yl8kQq2Xb7nHc1Rr",
                    "expiresIn": 600,
                }
            ]
        },
    )

    authorization_url: str = Field(
        description="Send the patient here to log in and consent. Returned rather "
        "than redirected to, so a caller that is not a browser can use it."
    )
    state: str = Field(
        description="The anti-CSRF nonce for this authorization. It comes back on "
        "the redirect and is what `POST /auth/callback` is given."
    )
    expires_in: int = Field(
        description="Seconds until the authorization attempt expires and the "
        "patient has to start again."
    )


class Connection(APIModel):
    """One authorized provider: this person, at this provider, on this server."""

    provider: str = Field(examples=["EPIC_SANDBOX"])
    iss: str = Field(
        description="The FHIR base URL this connection reads from.",
        examples=["https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"],
    )
    patient_fhir_id: str = Field(
        description="The patient's id **on that server**. Opaque, and meaningful "
        "only there: two servers can spell the same person differently, and the "
        "same string differently again.",
        examples=["erXuFYUfucBZaryVksYEcMg3"],
    )


class CallbackResponse(APIModel):
    """The result of a completed authorization."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "success": True,
                    "patientId": "pat_9Fq3TnVb2mKd7sXwLp4RcYhZ",
                    "sessionId": "8kQm2Xb7nHc1RrYl…",
                    "expiresIn": 3600,
                    "connection": {
                        "provider": "EPIC_SANDBOX",
                        "iss": "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
                        "patientFhirId": "erXuFYUfucBZaryVksYEcMg3",
                    },
                }
            ]
        },
    )

    success: bool = True
    patient_id: str = Field(
        description="The patient record this connection belongs to. Use it in the "
        "`/patients/{patientId}/…` paths.",
        examples=["pat_9Fq3TnVb2mKd7sXwLp4RcYhZ"],
    )
    session_id: str = Field(
        description="Present this as `Authorization: Bearer <sessionId>` to read "
        "the record, and to `POST /auth/connect` to add a second provider to the "
        "same patient record."
    )
    expires_in: int = Field(description="Seconds until the session expires.")
    connection: Connection


# --- the normalized record ---------------------------------------------------
#
# These restate the envelope app/fhir/normalize.py produces. The routes build
# them explicitly rather than returning the dicts, so the two drifting apart is a
# loud failure in the test suite instead of a field FastAPI quietly drops.


class Coding(APIModel):
    """A term as the server coded it: which vocabulary, which code, which label."""

    system: str | None = Field(
        default=None,
        description="The vocabulary the code belongs to.",
        examples=["http://snomed.info/sct"],
    )
    code: str | None = Field(default=None, examples=["55822004"])
    display: str | None = Field(default=None, examples=["Hyperlipidemia"])


class ResourceSummary(APIModel):
    """One resource, read into slots that mean the same thing for every type.

    `title`, `code`, `status`, `date` and `category` are comparable across
    resource types; `detail` is what differs between them, so its keys depend on
    the type. An Observation's is `{value, unit, components, interpretation,
    referenceRange}`; a Condition's is `{verificationStatus, severity,
    abatement}`.
    """

    id: str | None = None
    resource_type: str = Field(examples=["Condition"])
    title: str | None = Field(
        default=None,
        description="The server's own wording where it gave one, a coding's "
        "display otherwise.",
        examples=["Hyperlipidemia"],
    )
    code: Coding | None = None
    status: str | None = Field(default=None, examples=["active"])
    date: str | None = Field(
        default=None,
        description="When the resource applies to the patient, not when it was "
        "typed in: a Condition's onset ahead of its recorded date, an "
        "Observation's effective time ahead of when the lab released it.",
        examples=["2010-03-14"],
    )
    category: str | None = Field(default=None, examples=["problem-list-item"])
    detail: dict[str, Any] = Field(default_factory=dict)
    resource: dict[str, Any] = Field(
        description="The resource as the server sent it, validated against the "
        "FHIR R4B models. Nothing is lost to summarizing."
    )


class ResourceEnvelope(APIModel):
    """One resource type's worth of a read, whether or not it succeeded.

    A refusal, a timeout and an empty result all arrive with these same keys, so
    a caller never branches on shape to find out a read failed.
    """

    resource_type: str = Field(examples=["Condition"])
    status: Literal["ok", "error"]
    status_code: int | None = Field(
        default=None,
        description="The HTTP status the provider answered with. Null when "
        "nothing arrived at all.",
        examples=[200],
    )
    count: int = Field(description="Resources on this page.", examples=[2])
    total: int | None = Field(
        default=None,
        description="What the server says it holds. Null on the servers that "
        "omit `Bundle.total`.",
        examples=[17],
    )
    truncated: bool = Field(
        description="There is a next page. One page per resource type is read, "
        "so this reports that rather than presenting a partial list as a whole one."
    )
    skipped: int = Field(
        description="Entries that yielded no usable resource, so a partial "
        "record is visibly partial."
    )
    error: str | None = Field(
        default=None,
        description="Why the read failed: an OperationOutcome's own wording "
        "where the provider sent one, the status code otherwise.",
    )
    entries: list[ResourceSummary]


ConnectionStatus = Literal["ok", "degraded", "error"]
CONNECTION_STATUS_DESCRIPTION = (
    "`ok` when every resource type read cleanly, `degraded` when some did and "
    "some did not, `error` when none did."
)
NEEDS_REAUTHORIZATION_DESCRIPTION = (
    "The stored authorization has run out and cannot be renewed, so this "
    "connection will keep answering nothing until the patient connects the "
    "provider again. `status` says how much came back; this says whether "
    "anything but a fresh authorization would change that."
)


class ConnectionHealth(Connection):
    """How completely one connection could be read, and whether it still can be."""

    status: ConnectionStatus = Field(description=CONNECTION_STATUS_DESCRIPTION)
    error: str | None = Field(
        default=None,
        description="Why the connection as a whole could not be read. Per-type "
        "failures stay on their own envelope.",
    )
    needs_reauthorization: bool = Field(
        default=False, description=NEEDS_REAUTHORIZATION_DESCRIPTION
    )


class ConnectionResources(ConnectionHealth):
    """That, plus what the connection actually returned."""

    resources: dict[str, ResourceEnvelope] = Field(
        description="Keyed by resource type as this API names them, which is not "
        "always the FHIR type: the Observation searches are split by category, so "
        "`ObservationLaboratory` and `ObservationVitalSigns` keep separate buckets "
        "while both hold `Observation` entries."
    )


class ResourcesResponse(APIModel):
    """A patient's record, kept per connection."""

    patient_id: str = Field(examples=["pat_9Fq3TnVb2mKd7sXwLp4RcYhZ"])
    include: str = Field(
        description="The tier that was read, when no explicit types were named.",
        examples=["us-core"],
    )
    types: list[str] = Field(description="The resource types that were read.")
    connections: list[ConnectionResources] = Field(
        description="One entry per provider connected to this record. Resources "
        "stay separated by connection: the same person's Condition at two "
        "hospitals is two records on two servers, and merging them here would "
        "assert something the data cannot support."
    )


# --- the clinical summary ----------------------------------------------------


class SummaryItem(ResourceSummary):
    """A resource in a summary section, carrying which connection it came from."""

    provider: str = Field(
        description="The connection this came from. A merged section never loses "
        "provenance.",
        examples=["EPIC_SANDBOX"],
    )


class SummarySection(APIModel):
    """One part of the chart, merged across the record's connections."""

    key: str = Field(examples=["conditions"])
    title: str = Field(examples=["Conditions"])
    resource_types: list[str] = Field(
        description="The resource types that feed this section.",
        examples=[["Condition"]],
    )
    total: int = Field(
        description="How many the servers hold, summed across connections. Where "
        "a server omits `Bundle.total`, its own page count stands in.",
        examples=[14],
    )
    returned: int = Field(
        description="How many are in `items`, which is capped by `limit`.",
        examples=[14],
    )
    items: list[SummaryItem] = Field(description="Newest first; undated ones last.")


class Demographics(APIModel):
    """Who the patient is, as their connected providers describe them."""

    name: str | None = Field(default=None, examples=["Amy V. Shaw"])
    gender: str | None = Field(default=None, examples=["female"])
    birth_date: str | None = Field(default=None, examples=["1987-02-20"])
    deceased: bool | None = None
    sources: list[str] = Field(
        description="Every connection that supplied a Patient resource. The "
        "fields above come from the first of them rather than being merged "
        "field by field, which would describe a person no server actually did.",
        examples=[["EPIC_SANDBOX"]],
    )


class SummaryIssue(APIModel):
    """A resource type that could not be read, named by connection."""

    provider: str = Field(examples=["CERNER_SANDBOX"])
    type: str = Field(examples=["Immunization"])
    error: str = Field(examples=["Provider returned HTTP 503"])


class SummaryResponse(APIModel):
    """A patient's chart across everything they have connected."""

    patient_id: str = Field(examples=["pat_9Fq3TnVb2mKd7sXwLp4RcYhZ"])
    generated_at: str = Field(
        description="When this summary was assembled, in UTC. It is read live "
        "from the providers, not stored, so it is only true as of this moment.",
        examples=["2026-07-26T14:03:11+00:00"],
    )
    demographics: Demographics | None = Field(
        default=None,
        description="Null when no connection returned a readable Patient resource.",
    )
    connections: list[ConnectionHealth]
    sections: list[SummarySection] = Field(
        description="Always the same sections in the same order, present even "
        "when empty, so a consumer reads a fixed shape."
    )
    issues: list[SummaryIssue] = Field(
        description="What could not be read. A summary that quietly left these "
        "out would look the same as one for a patient who has none of it."
    )


# --- providers ---------------------------------------------------------------


class ConfiguredProvider(APIModel):
    """One endpoint this backend holds a registration for.

    Distinct from a search row: that describes an endpoint the national list
    knows about, which says nothing about whether we can authorize against it.
    This describes one we can, so every entry is connectable.
    """

    provider: str = Field(
        description="The provider key to pass to `POST /auth/connect`. It selects "
        "the client credentials and the issuers that are allowed.",
        examples=["EPIC_SANDBOX"],
    )
    iss: str = Field(
        description="The FHIR base URL to connect with, canonicalized to the exact "
        "form the provider's allowlist accepts back.",
        examples=["https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"],
    )
    name: str = Field(
        description="A display label for the entry, derived from the provider key.",
        examples=["Epic Sandbox"],
    )


class ProviderSearchRow(APIModel):
    """One endpoint from the national list."""

    idx: int = Field(description="Position in the full result set, for paging.")
    url: str = Field(
        description="The FHIR base URL. This is the `iss` to connect with.",
        examples=["https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"],
    )
    name: str = Field(
        description="The organization exposing the endpoint.",
        examples=["General Hospital"],
    )
    vendor: str | None = Field(
        default=None,
        description="The certified EHR developer behind the endpoint, which is "
        "what `vendor=` filters on.",
        examples=["Epic Systems Corporation"],
    )
    fhir_version: str | None = Field(
        default=None,
        description="The FHIR version the endpoint's capability statement "
        "declares. This backend reads R4.",
        examples=["4.0.1"],
    )
    smart_capable: bool | None = Field(
        default=None,
        description="Whether the endpoint served a SMART configuration when ONC "
        "last probed it. Null when the source could not say.",
    )
    configured: bool = Field(
        description="Whether this backend holds a registration that can "
        "authorize against this endpoint."
    )
    provider: str | None = Field(
        default=None,
        description="The provider key to pass to `POST /auth/connect`, on the "
        "endpoints this backend is registered for. Set only on an exact issuer "
        "match, never inferred from the vendor: two hospitals on the same EHR are "
        "different tenants with different logins and different registrations.",
        examples=["EPIC_SANDBOX"],
    )


class ProviderSearchResponse(APIModel):
    """A page of endpoint search results."""

    page: int
    page_size: int
    total_rows: int = Field(description="Matches across every page.")
    has_more: bool
    source: Literal["mirror", "fallback"] = Field(
        description="`mirror` when serving ONC's published file, `fallback` when "
        "that is unreachable and only this backend's configured providers are "
        "being offered."
    )
    data_date: str | None = Field(
        default=None,
        description="The date of the published file being served. Null for the "
        "fallback.",
        examples=["2025-11-14"],
    )
    rows: list[ProviderSearchRow]
