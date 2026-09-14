"""A fake identity provider, for driving the sign-in flow offline.

The login tests need an IdP that behaves like Okta or Google without being one.
This is that: a real RSA keypair, a real JWKS built from it, and real signed ID
tokens. Only the transport is faked, so what the tests exercise is the actual
verification path — signature, issuer, audience, expiry, nonce — rather than a
stand-in for it. A token this module signs with the wrong key, or for the wrong
audience, is rejected here for the same reason it would be in production.

The keypair is generated once per session because RSA key generation is the
slowest thing in the suite by an order of magnitude, and nothing under test
cares that two cases share a key.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import httpx
import jwt
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://idp.test/oauth2/default"
CLIENT_ID = "test-oidc-client-id"
CLIENT_SECRET = "test-oidc-client-secret"

AUTHORIZATION_ENDPOINT = f"{ISSUER}/v1/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/v1/token"
JWKS_URI = f"{ISSUER}/v1/keys"
END_SESSION_ENDPOINT = f"{ISSUER}/v1/logout"

KEY_ID = "test-signing-key"
# A second key the provider does not publish, for asserting that a token signed
# by something other than the IdP is refused.
IMPOSTOR_KEY_ID = "impostor-key"


@lru_cache(maxsize=None)
def _keypair(name: str) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signing_key() -> rsa.RSAPrivateKey:
    """The key the fake provider signs with, and publishes in its JWKS."""
    return _keypair("signing")


def impostor_key() -> rsa.RSAPrivateKey:
    """A key the provider never publishes."""
    return _keypair("impostor")


def jwks() -> dict[str, Any]:
    """The key set the provider publishes, as a JWKS document."""
    jwk = json.loads(RSAAlgorithm.to_jwk(signing_key().public_key()))
    jwk.update({"kid": KEY_ID, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


def discovery_document(issuer: str = ISSUER, **overrides: Any) -> dict[str, Any]:
    """What the provider publishes at ``.well-known/openid-configuration``."""
    return {
        "issuer": issuer,
        "authorization_endpoint": AUTHORIZATION_ENDPOINT,
        "token_endpoint": TOKEN_ENDPOINT,
        "jwks_uri": JWKS_URI,
        "end_session_endpoint": END_SESSION_ENDPOINT,
        "userinfo_endpoint": f"{issuer}/v1/userinfo",
        "response_types_supported": ["code"],
        "id_token_signing_alg_values_supported": ["RS256"],
        **overrides,
    }


def id_token(
    *,
    nonce: str,
    subject: str = "00u1a2b3c4d5e6f7g8h9",
    issuer: str = ISSUER,
    audience: str = CLIENT_ID,
    email: str | None = "avery@example.org",
    email_verified: bool = True,
    name: str | None = "Avery Diaz",
    expires_in: int = 3600,
    key: rsa.RSAPrivateKey | None = None,
    kid: str = KEY_ID,
    **claim_overrides: Any,
) -> str:
    """Mint a signed ID token. Every parameter is something a test bends."""
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "nonce": nonce,
    }
    if email is not None:
        claims["email"] = email
        claims["email_verified"] = email_verified
    if name is not None:
        claims["name"] = name
    claims.update(claim_overrides)
    # A None passed explicitly means "leave this claim out", which is how the
    # tests build a token missing a required one.
    claims = {key_: value for key_, value in claims.items() if value is not None}

    return jwt.encode(
        claims, key or signing_key(), algorithm="RS256", headers={"kid": kid}
    )


def token_response(id_token_value: str, **overrides: Any) -> dict[str, Any]:
    """What the token endpoint answers a successful code exchange with."""
    return {
        "access_token": "idp-access-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid profile email",
        "id_token": id_token_value,
        **overrides,
    }


@contextmanager
def serving_keys(monkeypatch, key_set: dict[str, Any] | None = None):
    """Answer JWKS fetches from memory instead of the network.

    ``PyJWKClient`` fetches with urllib rather than httpx, so respx cannot see
    it. ``fetch_data`` is the seam: stubbing it leaves the key-set parsing, the
    ``kid`` lookup and the signature check all real, and fakes only the request.
    """
    served = key_set if key_set is not None else jwks()
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: served)
    yield served


def mock_discovery(document: dict[str, Any] | None = None, *, status: int = 200) -> None:
    """Register the provider's discovery endpoint with respx."""
    body = discovery_document() if document is None else document
    respx.get(ISSUER + "/.well-known/openid-configuration").mock(
        return_value=httpx.Response(status, json=body)
    )


def mock_token_endpoint(response: dict[str, Any] | Any, *, status: int = 200) -> respx.Route:
    """Register the provider's token endpoint. Returns the route to assert on."""
    route = respx.post(TOKEN_ENDPOINT)
    if callable(response):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=httpx.Response(status, json=response))
    return route
