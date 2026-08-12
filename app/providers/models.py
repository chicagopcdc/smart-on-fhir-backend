"""Typed models for SMART discovery documents and OAuth token responses.

Kept separate from the provider base to avoid a circular import: discovery
imports SMARTConfiguration, and the provider base imports the discovery service.

SMART App Launch conformance:
https://hl7.org/fhir/smart-app-launch/conformance.html
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    model_validator,
)

_URL = TypeAdapter(HttpUrl)


def _is_unusable(value: Any, *, wants_list: bool) -> bool:
    """Whether a published value is junk rather than something to read.

    A list has to be a list of strings. Anything else is a URL, and the test for
    one is whether it parses as an absolute http(s) URL — asked of pydantic rather
    than reimplemented, so the answer here cannot drift from the answer the field
    itself would give, and so a value handed in already parsed still passes.

    Every published shape that has turned up in the wild lands on the same side of
    this: a null, an empty string, a relative path like ``/manage``, a ``urn:``, a
    list holding a list, a number. All of them mean the same thing — the server
    said something about an optional field and none of it can be read.
    """
    if value is None:
        return True
    if wants_list:
        return not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        )
    try:
        _URL.validate_python(value)
    except ValidationError:
        return True
    return False


class SMARTConfiguration(BaseModel):
    """A parsed ``.well-known/smart-configuration`` document.

    Only ``authorization_endpoint`` and ``token_endpoint`` are required; the
    rest are optional because conformant servers routinely omit them — and, it
    turns out, because they routinely publish them wrong. Optional here means both:
    a field that is absent and a field that is unusable are the same thing, since
    neither leaves us anything to read.

    The line that matters is what a value is *for*. The two endpoints are acted on
    — a user is redirected to one and credentials are posted to the other — so they
    are strict, and a relative or empty one is refused rather than guessed at.
    Everything else is description. Rejecting a whole document over the shape of a
    field nothing reads would refuse servers this backend can authorize against
    perfectly well.
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

    @model_validator(mode="before")
    @classmethod
    def _drop_unusable_description(cls, data: Any) -> Any:
        """Discard optional values that are not the shape they should be.

        Observed in the wild, on servers whose authorize and token endpoints are
        perfectly good: ``response_types_supported`` sent as ``null``,
        ``scopes_supported`` sent as a list containing a list, ``issuer`` sent as an
        empty string, and ``jwks_uri`` sent as a path relative to the base. Each
        would otherwise fail validation and take the whole document down with it,
        and none of them is read anywhere.

        Dropping a value falls back to the field's default, which is the same state
        as a server that never sent it — a path this backend already handles, since
        plenty of real servers advertise no PKCE and publish no revocation endpoint.
        So the result is a case already exercised, never a guess.
        """
        if not isinstance(data, dict):
            return data

        cleaned = dict(data)
        for name, field in cls.model_fields.items():
            if field.is_required() or name not in cleaned:
                continue
            wants_list = get_origin(field.annotation) is list
            if _is_unusable(cleaned[name], wants_list=wants_list):
                del cleaned[name]
        return cleaned

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
