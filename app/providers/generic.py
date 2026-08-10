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

    What the server actually said is carried on the exception rather than only
    written into its message, because one failure among these means something
    categorically different from the rest: RFC 6749 §5.2 spends ``invalid_grant``
    on a grant that will never work again, while a 503 says nothing about the
    grant at all. A caller that treated them alike would throw away a working
    refresh token every time a provider had a bad minute.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        oauth_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.oauth_error = oauth_error

    @property
    def grant_is_gone(self) -> bool:
        """True where the server said this authorization is finished.

        The error code carries the meaning and the status only corroborates it.
        RFC 6749 §5.2 pairs ``invalid_grant`` with a 400, but servers do not
        always: the SMART App Launcher answers a dead refresh token with a 401,
        and reading that as "we could not ask" would tell a patient to retry
        something that will never work again, and keep a spent secret on the row
        while it waited.

        Requiring the code rather than the status is also what keeps the
        distinction that matters. A 401 saying ``invalid_client`` is our
        credentials being wrong, not the patient's authorization being over, and
        must never cost anyone their connection. The 4xx bound is the last
        guard: a server error is never a statement about a grant.
        """
        if self.oauth_error != "invalid_grant":
            return False
        return self.status_code is not None and 400 <= self.status_code < 500


def _oauth_error(payload: object) -> str | None:
    """The ``error`` code out of an OAuth error response, where there is one.

    RFC 6749 §5.2 puts it in a JSON object. A server answering with HTML, with
    a bare string, or with nothing has not told us which failure this is, and
    None says so. Takes the parsed body rather than the response so that a body
    which is read on one path and unreadable on another is judged the same way.
    """
    return payload.get("error") if isinstance(payload, dict) else None


def _parsed(response: httpx.Response) -> object | None:
    """The response body as JSON, or None where it is not JSON at all."""
    try:
        return response.json()
    except ValueError:
        return None


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
        # aud must equal the FHIR base URL; normalize so a trailing slash never
        # produces an aud the authorization server fails to match.
        self.aud = aud.rstrip("/")
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

    async def revoke_token(
        self, config: SMARTConfiguration, token: str, *, token_type_hint: str
    ) -> bool:
        """Invalidate a token at the server, where the server offers a way to.

        RFC 7009. Most SMART servers publish no ``revocation_endpoint``, and
        there is nothing to be done about that from here, so a False return is a
        normal outcome rather than a failure. A 200 covers both a token that was
        revoked and one the server did not recognize, which is deliberate in the
        spec: distinguishing them would let a client probe for valid tokens.

        Revoking a refresh token is the one worth asking for — a server that
        supports it SHOULD invalidate the access tokens issued under the same
        grant along with it.
        """
        if config.revocation_endpoint is None:
            return False

        auth, extra_fields = self._client_authentication(config)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                str(config.revocation_endpoint),
                data={"token": token, "token_type_hint": token_type_hint, **extra_fields},
                auth=auth,
            )
        return response.status_code == 200

    async def _post_token(
        self, config: SMARTConfiguration, body: dict[str, str]
    ) -> TokenSet:
        auth, extra_fields = self._client_authentication(config)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                str(config.token_endpoint), data={**body, **extra_fields}, auth=auth
            )

        payload = _parsed(response)
        if response.status_code != 200:
            raise TokenExchangeError(
                f"token endpoint returned HTTP {response.status_code}",
                status_code=response.status_code,
                oauth_error=_oauth_error(payload),
            )
        if payload is None:
            raise TokenExchangeError(
                "token endpoint returned a non-JSON body", status_code=200
            )
        try:
            return TokenSet.model_validate(payload)
        except ValidationError as exc:
            # A 200 without an access_token — e.g. an OAuth error object.
            raise TokenExchangeError(
                "token endpoint response lacked a usable access token",
                status_code=200,
                oauth_error=_oauth_error(payload),
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
