"""A single discovery-driven SMART on FHIR provider.

One provider adapts to any SMART server by reading what its
``.well-known/smart-configuration`` advertises, rather than hardcoding a flow
per vendor:

* PKCE (``code_challenge`` with ``S256``) is added whenever the server
  advertises it — recommended for confidential clients too, and mandatory for
  public ones.
* Token-endpoint client authentication is chosen from
  ``token_endpoint_auth_methods_supported``: HTTP Basic, credentials in the
  form body, or — for a public client with no secret — none at all.

References: HL7 SMART App Launch (https://hl7.org/fhir/smart-app-launch/),
RFC 6749 §2.3.1 (client authentication), RFC 7636 (PKCE).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx
from pydantic import ValidationError

from app.providers.base import FHIRProvider
from app.providers.discovery import SMARTDiscovery
from app.providers.models import AuthorizationRequest, SMARTConfiguration, TokenSet


class SMARTProviderError(Exception):
    """Base class for provider-level failures."""


class TokenExchangeError(SMARTProviderError):
    """The token endpoint did not return a usable token set.

    Raised for protocol-level failures (a non-2xx status, a non-JSON body, or a
    response missing an ``access_token``). Transport failures surface as the
    underlying ``httpx.HTTPError`` so the caller can tell the two apart.
    """


class GenericSMARTProvider(FHIRProvider):
    """A confidential- or public-client SMART provider, configured per connection.

    The client credentials and ``redirect_uri`` come from the app's
    registration with the EHR; ``aud`` is the FHIR base URL the token will be
    scoped to (the issuer the app intends to call).
    """

    def __init__(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        aud: str,
        client_secret: str | None = None,
        discovery: SMARTDiscovery | None = None,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(discovery)
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.aud = aud
        self._timeout = timeout

    def build_auth_url(
        self,
        config: SMARTConfiguration,
        state: str,
        scopes: list[str],
    ) -> AuthorizationRequest:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            # Binds the eventual token to this FHIR server so it cannot be
            # replayed against another — the reason aud must track the issuer.
            "aud": self.aud,
        }

        code_verifier: str | None = None
        if config.supports_pkce:
            code_verifier, challenge = self._generate_pkce()
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"

        url = f"{config.authorization_endpoint}?{urlencode(params)}"
        return AuthorizationRequest(url=url, code_verifier=code_verifier)

    async def exchange_token(
        self,
        config: SMARTConfiguration,
        code: str,
        code_verifier: str | None = None,
    ) -> TokenSet:
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        if code_verifier:
            body["code_verifier"] = code_verifier
        return await self._post_token(config, body)

    async def refresh_token(
        self, config: SMARTConfiguration, refresh_token: str
    ) -> TokenSet:
        body = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        return await self._post_token(config, body)

    async def _post_token(
        self, config: SMARTConfiguration, body: dict[str, str]
    ) -> TokenSet:
        auth, extra_fields = self._client_authentication(config)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                str(config.token_endpoint), data={**body, **extra_fields}, auth=auth
            )

        if response.status_code != 200:
            raise TokenExchangeError(
                f"token endpoint returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TokenExchangeError("token endpoint returned a non-JSON body") from exc
        try:
            return TokenSet.model_validate(payload)
        except ValidationError as exc:
            # A 200 without an access_token — e.g. an OAuth error object.
            raise TokenExchangeError(
                "token endpoint response lacked a usable access token"
            ) from exc

    def _client_authentication(
        self, config: SMARTConfiguration
    ) -> tuple[tuple[str, str] | None, dict[str, str]]:
        """Choose token-endpoint client auth from the server's advertised methods.

        Returns the httpx ``auth`` pair (for HTTP Basic) and any extra form
        fields to merge into the request body. PKCE is handled separately.
        """
        methods = config.token_endpoint_auth_methods_supported

        if self.client_secret is not None:
            # Confidential client. Prefer Basic — SMART's recommended default —
            # then form-post. With nothing advertised, Basic is the safe
            # assumption. But if the server advertises methods and none is a
            # symmetric-secret one (e.g. only private_key_jwt), do not blindly
            # send Basic to a server that will reject it.
            if "client_secret_basic" in methods or not methods:
                return (self.client_id, self.client_secret), {}
            if "client_secret_post" in methods:
                return None, {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                }
            raise SMARTProviderError(
                "token endpoint advertises no client-secret authentication "
                f"method this provider supports: {methods}"
            )

        # Public client: no secret to present. The client_id identifies the app
        # and PKCE proves possession of the authorization request.
        return None, {"client_id": self.client_id}

    @staticmethod
    def _generate_pkce() -> tuple[str, str]:
        """Return a (verifier, S256 challenge) pair per RFC 7636."""
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return verifier, challenge
