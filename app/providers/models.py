"""Typed models for SMART discovery documents and OAuth token responses.

Kept separate from the provider base to avoid a circular import: discovery
imports SMARTConfiguration, and the provider base imports the discovery service.

SMART App Launch conformance:
https://hl7.org/fhir/smart-app-launch/conformance.html
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
)

_URL = TypeAdapter(HttpUrl)


def _url_or_none(value: Any) -> Any:
    """Keep a value only if it is a usable absolute URL.

    Whether it is one is asked of pydantic rather than reimplemented, so the answer
    cannot drift from what the field itself would accept, and a value handed in
    already parsed still passes.
    """
    try:
        _URL.validate_python(value)
    except ValidationError:
        return None
    return value


def _strings_or_empty(value: Any) -> Any:
    """Keep a list only if every item in it is a string."""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


# Optional fields opt into leniency by using these, so the rule is visible where a
# field is declared rather than applied to everything by a rule elsewhere. A future
# field of some other type then behaves the way its annotation says.
LenientUrl = Annotated[HttpUrl | None, BeforeValidator(_url_or_none)]
LenientStrings = Annotated[list[str], BeforeValidator(_strings_or_empty)]


class SMARTConfiguration(BaseModel):
    """A parsed ``.well-known/smart-configuration`` document.

    Only ``authorization_endpoint`` and ``token_endpoint`` are required; the rest
    are optional because conformant servers routinely omit them — and, it turns out,
    because they routinely publish them wrong. Both mean the same thing here, since
    neither leaves anything to read.

    The line is what a value is *for*: the two endpoints are acted on — a user is
    redirected to one, credentials posted to the other — so a relative or empty one
    is refused. Everything else is description, and rejecting a document over a
    field nothing reads would refuse servers we can authorize against perfectly
    well. Seen in the wild on servers with good endpoints:
    ``response_types_supported`` as ``null``, ``scopes_supported`` as a list of
    lists, ``issuer`` as an empty string, ``jwks_uri`` as a relative path. Each is
    dropped to the field's default, which is the state a server that never sent it
    would leave us in — a case this backend already handles.
    """

    # Tolerate vendor-specific keys some servers include.
    model_config = ConfigDict(extra="ignore")

    # Endpoint URLs must be absolute per the spec; HttpUrl rejects relative or
    # malformed values before we redirect a user or post credentials to them.
    authorization_endpoint: HttpUrl
    token_endpoint: HttpUrl

    issuer: LenientUrl = None
    jwks_uri: LenientUrl = None
    introspection_endpoint: LenientUrl = None
    revocation_endpoint: LenientUrl = None
    management_endpoint: LenientUrl = None
    registration_endpoint: LenientUrl = None

    grant_types_supported: LenientStrings = Field(default_factory=list)
    scopes_supported: LenientStrings = Field(default_factory=list)
    response_types_supported: LenientStrings = Field(default_factory=list)
    capabilities: LenientStrings = Field(default_factory=list)
    token_endpoint_auth_methods_supported: LenientStrings = Field(default_factory=list)
    code_challenge_methods_supported: LenientStrings = Field(default_factory=list)

    @property
    def supports_pkce(self) -> bool:
        """True if the server advertises PKCE with SHA-256 (S256)."""
        return "S256" in self.code_challenge_methods_supported


class TokenSet(BaseModel):
    """An OAuth2 token response from a SMART server's token endpoint.

    ``expires_in`` is kept as the raw seconds value; converting it to an
    absolute expiry happens where tokens are persisted.
    """

    model_config = ConfigDict(extra="ignore")

    access_token: str
    token_type: str = "Bearer"
    expires_in: int | None = None
    scope: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    patient: str | None = None  # FHIR id of the authorized patient


@dataclass(frozen=True)
class AuthorizationRequest:
    """The result of building an authorization redirect.

    ``code_verifier`` is the PKCE secret minted alongside the URL when the
    server advertises PKCE; it is ``None`` otherwise. It must be carried to the
    token exchange but never sent to the browser, so it is stored server-side
    with the OAuth ``state``.
    """

    url: str
    code_verifier: str | None = None
