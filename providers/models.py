"""Typed models for SMART discovery documents and OAuth token responses.

Kept separate from the provider base to avoid a circular import: discovery
imports SMARTConfiguration, and the provider base imports the discovery service.

SMART App Launch conformance:
https://hl7.org/fhir/smart-app-launch/conformance.html
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SMARTConfiguration(BaseModel):
    """A parsed ``.well-known/smart-configuration`` document.

    Only ``authorization_endpoint`` and ``token_endpoint`` are required; the
    rest are optional because conformant servers routinely omit them.
    """

    # Tolerate vendor-specific keys some servers include.
    model_config = ConfigDict(extra="ignore")

    # Endpoint URLs must be absolute per the spec; HttpUrl rejects relative or
    # malformed values before we redirect a user or post credentials to them.
    authorization_endpoint: HttpUrl
    token_endpoint: HttpUrl

    issuer: HttpUrl | None = None
    jwks_uri: HttpUrl | None = None
    introspection_endpoint: HttpUrl | None = None
    revocation_endpoint: HttpUrl | None = None
    management_endpoint: HttpUrl | None = None
    registration_endpoint: HttpUrl | None = None

    grant_types_supported: list[str] = Field(default_factory=list)
    scopes_supported: list[str] = Field(default_factory=list)
    response_types_supported: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    token_endpoint_auth_methods_supported: list[str] = Field(default_factory=list)
    code_challenge_methods_supported: list[str] = Field(default_factory=list)

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
