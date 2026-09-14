"""The identity provider users sign in with, as a plain OIDC relying party.

Who someone is and which medical records they connected are different questions,
answered by different servers. This module answers the first. It knows nothing
about FHIR, and the tokens it handles are never used to read anything: an ID
token is read once, for the ``sub`` inside it, and then discarded.

There is one implementation rather than one per vendor, for the same reason
``app/providers/generic.py`` has one for SMART servers: Okta and Google are both
ordinary OIDC providers, so everything that differs between them — where to send
the user, where to exchange the code, which keys sign the tokens — is published
at the issuer's ``.well-known/openid-configuration`` and read at runtime. Which
one a deployment uses is then a matter of setting ``OIDC_ISSUER``.

References: OpenID Connect Core 1.0 (§2 ID token, §3.1 authorization code flow,
§3.1.3.7 ID token validation), OpenID Connect Discovery 1.0, RFC 7636 (PKCE).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import get_settings

_WELL_KNOWN_PATH = "/.well-known/openid-configuration"


class OIDCError(Exception):
    """Base class for every way signing a user in can fail."""


class OIDCNotConfigured(OIDCError):
    """No identity provider is configured, so there is nothing to sign in to."""


class OIDCDiscoveryError(OIDCError):
    """The provider's OpenID configuration could not be read or understood."""


class OIDCExchangeError(OIDCError):
    """The token endpoint refused the code, or answered with nothing usable."""


class IDTokenError(OIDCError):
    """The ID token was missing, unverifiable, or not the one we asked for.

    Kept apart from an exchange failure because the two mean opposite things
    about the provider. A refused exchange is a routine answer — a stale code, a
    user who took too long. An ID token that does not verify came back from a
    successful exchange and should never happen, so it is worth seeing as its
    own thing rather than folded in with the ordinary case.
    """


@dataclass(frozen=True)
class OIDCConfiguration:
    """The endpoints an issuer publishes, as much of them as we use."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    # Optional in the spec and absent from some deployments. Only the logout
    # endpoint is reached for, and only to hand the frontend somewhere to send
    # the browser; nothing depends on either being there.
    end_session_endpoint: str | None = None
    userinfo_endpoint: str | None = None


@dataclass(frozen=True)
class Identity:
    """Who the provider says this is, reduced to what an account needs.

    ``issuer`` and ``subject`` together are the identity; everything else is
    presentational. See ``User`` for why email cannot be the key.
    """

    issuer: str
    subject: str
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None


@dataclass(frozen=True)
class LoginRequest:
    """Where to send the user, and the secrets the way back must produce."""

    url: str
    state: str
    nonce: str
    code_verifier: str


def generate_pkce(verifier: str) -> str:
    """The S256 challenge for a verifier, per RFC 7636 §4.2."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OIDCClient:
    """Signs users in against one configured issuer.

    Holds a cache of the issuer's configuration and a cache of its signing keys,
    both of which are stable for long stretches and neither of which is worth
    fetching on every login. One instance is shared by the process; see
    ``client()`` below.
    """

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        redirect_uri: str,
        client_secret: str | None = None,
        scopes: str = "openid profile email",
        timeout: float = 15.0,
        config_cache_ttl: float = 900.0,
        clock_skew_seconds: int = 60,
    ) -> None:
        # Normalized once so the configuration URL, the `iss` we compare an ID
        # token against, and the cache key all agree on one spelling.
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self._timeout = timeout
        self._config_cache_ttl = config_cache_ttl
        self._clock_skew = clock_skew_seconds
        self._config: tuple[float, OIDCConfiguration] | None = None
        self._jwks: PyJWKClient | None = None

    def clear_cache(self) -> None:
        """Drop the cached configuration and signing keys."""
        self._config = None
        self._jwks = None

    # --- discovery -------------------------------------------------------

    async def discover(self) -> OIDCConfiguration:
        """Read the issuer's OpenID configuration, from cache where it is fresh."""
        if self._config is not None:
            expires_at, config = self._config
            if expires_at > time.monotonic():
                return config

        url = self.issuer + _WELL_KNOWN_PATH
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise OIDCDiscoveryError(
                f"{url} returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise OIDCDiscoveryError(f"Could not reach {url}: {exc!r}") from exc
        except ValueError as exc:
            raise OIDCDiscoveryError(f"{url} did not return JSON") from exc

        if not isinstance(payload, dict):
            raise OIDCDiscoveryError(f"{url} did not return a JSON object")

        # An issuer that does not match the one we asked is the discovery
        # document of some other provider, whatever it says about itself
        # (Discovery §4.3). Trusting it would mean sending users to endpoints
        # this deployment never configured.
        published = str(payload.get("issuer", "")).rstrip("/")
        if published != self.issuer:
            raise OIDCDiscoveryError(
                f"{url} is published by {published or 'no issuer'}, not {self.issuer}"
            )

        missing = [
            key
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri")
            if not payload.get(key)
        ]
        if missing:
            raise OIDCDiscoveryError(
                f"{url} is missing {', '.join(missing)}, which a login cannot proceed without"
            )

        config = OIDCConfiguration(
            issuer=published,
            authorization_endpoint=str(payload["authorization_endpoint"]),
            token_endpoint=str(payload["token_endpoint"]),
            jwks_uri=str(payload["jwks_uri"]),
            end_session_endpoint=_optional_str(payload.get("end_session_endpoint")),
            userinfo_endpoint=_optional_str(payload.get("userinfo_endpoint")),
        )
        if self._config_cache_ttl > 0:
            self._config = (time.monotonic() + self._config_cache_ttl, config)
        return config

    # --- the two halves of a login ---------------------------------------

    def build_login_url(
        self,
        config: OIDCConfiguration,
        *,
        state: str,
        nonce: str,
        code_verifier: str,
    ) -> str:
        """Where to send the user to sign in.

        PKCE is always sent rather than conditional on what the provider
        advertises, which is the opposite of the SMART path. There the servers
        are someone else's and vary; here the issuer is one this deployment
        chose, Okta and Google both support S256, and a provider that ignores the
        challenge is no worse off for having been sent one.
        """
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": generate_pkce(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{config.authorization_endpoint}?{urlencode(params)}"

    async def exchange_code(
        self, config: OIDCConfiguration, code: str, code_verifier: str
    ) -> dict[str, Any]:
        """Trade the authorization code for the token response containing the ID token."""
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.client_id,
        }
        # A confidential client authenticates with Basic, which is what both Okta
        # and Google accept and what RFC 6749 §2.3.1 prefers. A public client has
        # no secret and leans on PKCE, which is why client_id is in the body
        # either way.
        auth = (self.client_id, self.client_secret) if self.client_secret else None

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    config.token_endpoint, data=body, auth=auth
                )
        except httpx.HTTPError as exc:
            raise OIDCExchangeError(f"Could not reach the token endpoint: {exc!r}") from exc

        payload = _json_or_none(response)
        if response.status_code != 200:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise OIDCExchangeError(
                f"token endpoint returned HTTP {response.status_code}"
                + (f" ({error})" if error else "")
            )
        if not isinstance(payload, dict):
            raise OIDCExchangeError("token endpoint returned a non-JSON body")
        return payload

    async def identity_from(
        self, config: OIDCConfiguration, token_response: dict[str, Any], *, nonce: str
    ) -> Identity:
        """Verify the ID token in a token response and read the identity out of it.

        Every check OIDC Core §3.1.3.7 asks for that is ours to make: the
        signature against the issuer's published keys, the issuer, the audience,
        the expiry, and the nonce. The signature is checked even though the token
        came back over TLS from a direct call — the spec allows skipping it in
        that case, but the check costs one cached key lookup and it is the
        difference between trusting a response and trusting a network.
        """
        id_token = token_response.get("id_token")
        if not id_token:
            raise IDTokenError("the token response carried no id_token")

        try:
            # PyJWKClient fetches with urllib, which would block the event loop
            # for the length of a call to the IdP on every cache miss.
            key = await asyncio.to_thread(self._signing_key, config, id_token)
        except jwt.PyJWKClientConnectionError as exc:
            # Reaching the key set failed, which says nothing about the token.
            # Reported as an upstream fault so the caller is told to try again
            # rather than that their sign-in was rejected.
            raise OIDCDiscoveryError(
                f"Could not read the signing keys at {config.jwks_uri}: {exc}"
            ) from exc
        except jwt.PyJWKClientError as exc:
            # The key set was read and holds nothing matching this token's kid.
            # That is a statement about the token, not about the network: the
            # provider does not publish whatever signed it. Refusing it is the
            # point of checking at all, so it must not be softened into "try
            # again shortly" — which is what folding it in with the case above
            # would do.
            raise IDTokenError(
                f"the id_token was signed by a key {config.issuer} does not publish"
            ) from exc

        try:
            claims = jwt.decode(
                id_token,
                key,
                algorithms=_ALLOWED_ALGORITHMS,
                audience=self.client_id,
                issuer=config.issuer,
                leeway=self._clock_skew,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise IDTokenError(f"the id_token did not verify: {exc}") from exc

        # The nonce ties this token to the login that asked for it, so one
        # obtained through any other flow cannot be injected into this one.
        # Compared here rather than by the decoder, which has no notion of it.
        if claims.get("nonce") != nonce:
            raise IDTokenError("the id_token was issued for a different login")

        subject = claims.get("sub")
        if not subject:
            raise IDTokenError("the id_token carried no subject")

        return Identity(
            issuer=config.issuer,
            subject=str(subject),
            email=_optional_str(claims.get("email")),
            email_verified=bool(claims.get("email_verified", False)),
            display_name=_optional_str(claims.get("name")),
        )

    def _signing_key(self, config: OIDCConfiguration, id_token: str):
        """The provider's key that signed this token, fetched and cached by kid.

        ``PyJWKClient`` does its own fetching and caching, including re-fetching
        when a token arrives signed by a key it has not seen — which is what
        makes an IdP rotating its keys a non-event rather than an outage.
        """
        if self._jwks is None:
            self._jwks = PyJWKClient(config.jwks_uri, cache_keys=True)
        return self._jwks.get_signing_key_from_jwt(id_token).key

    def logout_url(self, config: OIDCConfiguration, *, id_token: str | None = None) -> str | None:
        """Where to send the browser to end the session at the provider too.

        Null where the provider publishes no ``end_session_endpoint``, which is
        an ordinary thing for it not to publish. Signing out of this application
        never depends on it: the session here is deleted either way, and this is
        only the extra step that stops the IdP from signing the user straight
        back in without asking.
        """
        if config.end_session_endpoint is None:
            return None
        params: dict[str, str] = {
            "post_logout_redirect_uri": get_settings().frontend_hostname.rstrip("/")
        }
        if id_token:
            params["id_token_hint"] = id_token
        else:
            # Okta accepts this in place of a hint; providers that do not simply
            # ignore it and ask the user which session to end.
            params["client_id"] = self.client_id
        return f"{config.end_session_endpoint}?{urlencode(params)}"


# Asymmetric algorithms only. An ID token is verified against keys the issuer
# publishes, so an HMAC algorithm has no place here — accepting one is the
# classic confusion where a published public key is replayed as a shared secret.
_ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256"]


def _optional_str(value: object) -> str | None:
    """A claim as a string, or None where it is absent or empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_or_none(response: httpx.Response) -> object | None:
    """The response body as JSON, or None where it is not JSON at all."""
    try:
        return response.json()
    except ValueError:
        return None


# One client per process, built on first use so that a deployment with no IdP
# configured never constructs one. Reset by tests, and by any change to the
# settings it was built from.
_client: OIDCClient | None = None


def client() -> OIDCClient:
    """The configured OIDC client, or raise if this deployment has no IdP.

    Every login endpoint goes through here, so "not configured" is answered in
    one place rather than checked at each of them.
    """
    global _client
    settings = get_settings()
    if not settings.user_login_configured:
        raise OIDCNotConfigured(
            "No identity provider is configured; set OIDC_ISSUER and OIDC_CLIENT_ID"
        )
    if _client is None:
        _client = OIDCClient(
            # Both are known non-null: user_login_configured is what checked them.
            issuer=settings.oidc_issuer,  # type: ignore[arg-type]
            client_id=settings.oidc_client_id,  # type: ignore[arg-type]
            client_secret=settings.oidc_client_secret,
            redirect_uri=settings.oidc_redirect_uri,
            scopes=settings.oidc_scopes,
            clock_skew_seconds=settings.oidc_clock_skew_seconds,
        )
    return _client


def reset_client() -> None:
    """Forget the built client, so the next call reads the settings again."""
    global _client
    _client = None
