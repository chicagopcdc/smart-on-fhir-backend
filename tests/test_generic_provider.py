"""How the generic provider adapts to a server's advertised capabilities.

These drive the two decisions the discovery-driven design exists to make — what
the authorization request looks like, and how the token exchange authenticates —
against the real Epic and public SMART discovery documents.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.providers.generic import (
    GenericSMARTProvider,
    SMARTProviderError,
    TokenExchangeError,
)
from app.providers.models import SMARTConfiguration

EPIC_ISS = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
EPIC_AUTHORIZE_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize"
EPIC_TOKEN_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token"
REDIRECT_URI = "http://localhost:3000/auth/callback"


def _config(fixture: dict, **overrides) -> SMARTConfiguration:
    return SMARTConfiguration.model_validate({**fixture, **overrides})


def _provider(config_secret: str | None = "client-secret") -> GenericSMARTProvider:
    return GenericSMARTProvider(
        client_id="client-id",
        client_secret=config_secret,
        redirect_uri=REDIRECT_URI,
        aud=EPIC_ISS,
    )


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# Building the authorization request.


def test_auth_url_targets_discovered_endpoint_and_derives_aud_from_iss(epic_smart_config):
    config = _config(epic_smart_config)

    auth = _provider().build_auth_url(config, state="state-123", scopes=["openid", "launch/patient"])
    query = parse_qs(urlparse(auth.url).query)

    assert auth.url.startswith(EPIC_AUTHORIZE_URL)
    # aud is the issuer the token will be used against — derived, never hardcoded.
    assert query["aud"] == [EPIC_ISS]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["state"] == ["state-123"]
    assert query["scope"] == ["openid launch/patient"]


def test_auth_url_adds_pkce_when_server_advertises_s256(epic_smart_config):
    config = _config(epic_smart_config)
    assert config.supports_pkce is True

    auth = _provider().build_auth_url(config, state="s", scopes=["openid"])
    query = parse_qs(urlparse(auth.url).query)

    assert query["code_challenge_method"] == ["S256"]
    assert auth.code_verifier is not None
    # The challenge is the base64url SHA-256 of the verifier we kept.
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(auth.code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert query["code_challenge"] == [expected]


def test_auth_url_omits_pkce_when_not_advertised(epic_smart_config):
    config = _config(epic_smart_config, code_challenge_methods_supported=[])

    auth = _provider().build_auth_url(config, state="s", scopes=["openid"])
    query = parse_qs(urlparse(auth.url).query)

    assert "code_challenge" not in query
    assert auth.code_verifier is None


# Exchanging the code: client authentication chosen from discovery.


@respx.mock
async def test_exchange_uses_basic_auth_when_secret_and_basic_advertised(
    epic_smart_config, epic_token_response
):
    config = _config(epic_smart_config)  # advertises client_secret_basic
    route = respx.post(EPIC_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=epic_token_response)
    )

    token = await _provider().exchange_token(config, code="auth-code", code_verifier="verifier-abc")

    request = route.calls.last.request
    scheme, encoded = request.headers["Authorization"].split(" ", 1)
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == "client-id:client-secret"

    body = _form(request)
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "auth-code"
    assert body["redirect_uri"] == REDIRECT_URI
    assert body["code_verifier"] == "verifier-abc"  # PKCE secret replayed
    assert "client_secret" not in body  # held in the Basic header, not the body

    # TokenSet keeps the patient and drops vendor-specific keys.
    assert token.patient == epic_token_response["patient"]
    assert token.access_token == epic_token_response["access_token"]
    assert not hasattr(token, "__epic.dstu2.patient")


@respx.mock
async def test_exchange_posts_credentials_when_only_post_advertised(
    epic_smart_config, epic_token_response
):
    config = _config(epic_smart_config, token_endpoint_auth_methods_supported=["client_secret_post"])
    route = respx.post(EPIC_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=epic_token_response)
    )

    await _provider().exchange_token(config, code="auth-code")

    request = route.calls.last.request
    assert "Authorization" not in request.headers
    body = _form(request)
    assert body["client_id"] == "client-id"
    assert body["client_secret"] == "client-secret"


@respx.mock
async def test_public_client_sends_client_id_in_body_and_no_auth_header(
    public_smart_config, epic_token_response
):
    config = _config(public_smart_config)
    token_url = str(config.token_endpoint)
    route = respx.post(token_url).mock(
        return_value=httpx.Response(200, json=epic_token_response)
    )

    provider = GenericSMARTProvider(
        client_id="public-app", client_secret=None, redirect_uri=REDIRECT_URI, aud=EPIC_ISS
    )
    # No secret, but PKCE is available and carried through.
    auth = provider.build_auth_url(config, state="s", scopes=["openid"])
    assert auth.code_verifier is not None
    await provider.exchange_token(config, code="auth-code", code_verifier=auth.code_verifier)

    request = route.calls.last.request
    assert "Authorization" not in request.headers
    body = _form(request)
    assert body["client_id"] == "public-app"
    assert "client_secret" not in body


@respx.mock
async def test_refresh_token_posts_refresh_grant(epic_smart_config, epic_token_response):
    config = _config(epic_smart_config)
    route = respx.post(EPIC_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=epic_token_response)
    )

    token = await _provider().refresh_token(config, refresh_token="refresh-abc")

    body = _form(route.calls.last.request)
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "refresh-abc"
    assert token.access_token == epic_token_response["access_token"]


# Failure paths surface a typed error, not a raw HTTP/validation exception.


async def test_exchange_refuses_basic_when_server_advertises_no_supported_method(
    epic_smart_config,
):
    # A confidential client facing a server that offers only asymmetric auth:
    # we must not blindly send Basic to a server that will reject it.
    config = _config(
        epic_smart_config, token_endpoint_auth_methods_supported=["private_key_jwt"]
    )

    with pytest.raises(SMARTProviderError):
        await _provider().exchange_token(config, code="auth-code")


@respx.mock
async def test_exchange_raises_on_non_200(epic_smart_config):
    config = _config(epic_smart_config)
    respx.post(EPIC_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )

    with pytest.raises(TokenExchangeError):
        await _provider().exchange_token(config, code="bad-code")


@respx.mock
async def test_exchange_raises_when_response_lacks_access_token(epic_smart_config):
    config = _config(epic_smart_config)
    respx.post(EPIC_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"token_type": "Bearer"})
    )

    with pytest.raises(TokenExchangeError):
        await _provider().exchange_token(config, code="auth-code")


# One of these failures means a grant will never work again and the rest do not.
# Everything downstream turns on the difference — whether a stored refresh token
# is kept or thrown away — so it is pinned here rather than left to a message.


@pytest.mark.parametrize(
    ("response", "gone"),
    [
        # What RFC 6749 §5.2 prescribes.
        (httpx.Response(400, json={"error": "invalid_grant"}), True),
        # And what the SMART App Launcher actually answers a dead refresh token
        # with, verbatim. Reading the status instead of the code would tell a
        # patient to retry something that can never work again.
        (
            httpx.Response(
                401,
                json={"error": "invalid_grant", "error_description": "Invalid refresh token"},
            ),
            True,
        ),
        # Our credentials are wrong, not the patient's authorization — the same
        # 401 as above, and the reason the code has to be what decides. Reading
        # this as a dead grant would cost every connection in the deployment the
        # next time a client secret was rotated badly.
        (httpx.Response(401, json={"error": "invalid_client"}), False),
        # The server is unwell and has said nothing about the grant at all.
        (httpx.Response(503, text="down for maintenance"), False),
        # A 5xx is never a statement about a grant, whatever it carries.
        (httpx.Response(500, json={"error": "invalid_grant"}), False),
        # A refusal with no machine-readable reason is not one we can act on.
        (httpx.Response(400, text="<html>Bad Request</html>"), False),
        # A 200 carrying an error object instead of a token set.
        (httpx.Response(200, json={"error": "invalid_grant"}), False),
    ],
)
@respx.mock
async def test_only_an_invalid_grant_means_the_authorization_is_finished(
    epic_smart_config, response, gone
):
    config = _config(epic_smart_config)
    respx.post(EPIC_TOKEN_URL).mock(return_value=response)

    with pytest.raises(TokenExchangeError) as raised:
        await _provider().refresh_token(config, refresh_token="refresh-abc")

    assert raised.value.grant_is_gone is gone
