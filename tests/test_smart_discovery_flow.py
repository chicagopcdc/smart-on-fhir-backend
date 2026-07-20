"""Discovery flow: from an issuer URL to a usable SMART configuration.

Drives the path a real request takes — fetch the server's
``.well-known/smart-configuration`` through ``FHIRProvider.discover()``, parse
it, and read the endpoints and PKCE signal an authorization request needs —
against the real Epic and public discovery documents, with HTTP mocked by respx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.providers.discovery import (
    DiscoveryNotFoundError,
    DiscoveryParseError,
    DiscoveryUnreachableError,
    SMARTDiscovery,
)

WELL_KNOWN = "/.well-known/smart-configuration"

EPIC_ISS = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
PUBLIC_ISS = "https://launch.smarthealthit.org/v/r4/fhir"
CERNER_ISS = (
    "https://fhir-ehr-code.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d"
)


# Happy path: discover a real server, then use what came back.


@respx.mock
async def test_discovering_epic_yields_inputs_for_authorization(
    make_provider, epic_smart_config
):
    route = respx.get(EPIC_ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=epic_smart_config)
    )

    provider = make_provider()
    config = await provider.discover(EPIC_ISS)

    assert route.called
    assert str(config.authorization_endpoint).endswith("/oauth2/authorize")
    assert str(config.token_endpoint).endswith("/oauth2/token")
    assert config.supports_pkce is True
    assert "client_secret_basic" in config.token_endpoint_auth_methods_supported

    # The auth URL is rooted at the discovered endpoint, not a hardcoded one.
    auth = provider.build_auth_url(config, state="abc123", scopes=["openid"])
    assert auth.url.startswith(str(config.authorization_endpoint))
    assert "state=abc123" in auth.url


@respx.mock
async def test_discovering_public_server_yields_inputs_for_authorization(
    make_provider, public_smart_config
):
    route = respx.get(PUBLIC_ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=public_smart_config)
    )

    config = await make_provider().discover(PUBLIC_ISS)

    assert route.called
    assert str(config.authorization_endpoint).endswith("/auth/authorize")
    assert str(config.token_endpoint).endswith("/auth/token")
    assert config.supports_pkce is True
    assert str(config.introspection_endpoint).endswith("/auth/introspect")
    assert "launch/patient" in config.scopes_supported


@respx.mock
async def test_issuer_trailing_slash_is_normalized(make_provider, public_smart_config):
    route = respx.get(PUBLIC_ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=public_smart_config)
    )

    await make_provider().discover(PUBLIC_ISS + "/")

    assert route.called


# Caching: repeated discovery of the same issuer doesn't re-hit the server.


@respx.mock
async def test_repeat_discovery_is_served_from_cache(make_provider, epic_smart_config):
    route = respx.get(EPIC_ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=epic_smart_config)
    )

    provider = make_provider()
    await provider.discover(EPIC_ISS)
    await provider.discover(EPIC_ISS)

    assert route.call_count == 1


@respx.mock
async def test_discovery_refetches_when_cache_disabled(make_provider, epic_smart_config):
    route = respx.get(EPIC_ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=epic_smart_config)
    )

    provider = make_provider(SMARTDiscovery(cache_ttl=0))
    await provider.discover(EPIC_ISS)
    await provider.discover(EPIC_ISS)

    assert route.call_count == 2


# Failure paths: the flow raises a typed error, not a raw HTTP/JSON exception.


@respx.mock
async def test_missing_smart_configuration_raises_not_found(make_provider):
    iss = "https://no-smart.example/fhir"
    respx.get(iss + WELL_KNOWN).mock(return_value=httpx.Response(404))

    with pytest.raises(DiscoveryNotFoundError):
        await make_provider().discover(iss)


@respx.mock
async def test_server_error_raises_unreachable(make_provider):
    iss = "https://flaky.example/fhir"
    respx.get(iss + WELL_KNOWN).mock(return_value=httpx.Response(503))

    with pytest.raises(DiscoveryUnreachableError):
        await make_provider().discover(iss)


@respx.mock
async def test_network_failure_raises_unreachable(make_provider):
    iss = "https://down.example/fhir"
    respx.get(iss + WELL_KNOWN).mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(DiscoveryUnreachableError):
        await make_provider().discover(iss)


@respx.mock
async def test_non_json_body_raises_parse_error(make_provider):
    iss = "https://weird.example/fhir"
    respx.get(iss + WELL_KNOWN).mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )

    with pytest.raises(DiscoveryParseError):
        await make_provider().discover(iss)


@respx.mock
async def test_doc_missing_required_endpoint_raises_parse_error(make_provider):
    iss = "https://incomplete.example/fhir"
    # Missing token_endpoint -> unusable for the flow.
    respx.get(iss + WELL_KNOWN).mock(
        return_value=httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://incomplete.example/auth",
                "code_challenge_methods_supported": ["S256"],
            },
        )
    )

    with pytest.raises(DiscoveryParseError):
        await make_provider().discover(iss)


# Opt-in: the same flow against the real servers. Run with `pytest -m live`.


@pytest.mark.live
async def test_live_discovery_against_real_servers(make_provider):
    provider = make_provider()

    epic = await provider.discover(EPIC_ISS)
    assert epic.supports_pkce
    assert str(epic.token_endpoint).endswith("/oauth2/token")

    public = await provider.discover(PUBLIC_ISS)
    assert public.supports_pkce
    assert str(public.token_endpoint).endswith("/auth/token")

    # Cerner / Oracle Health advertises S256 too, so the adapter enables PKCE
    # against it with no vendor-specific handling.
    cerner = await provider.discover(CERNER_ISS)
    assert cerner.supports_pkce
    assert "client_secret_basic" in cerner.token_endpoint_auth_methods_supported
