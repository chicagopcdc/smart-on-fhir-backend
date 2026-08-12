"""Discovery flow: from an issuer URL to a usable SMART configuration.

Drives the path a real request takes — decide whether the issuer is one we are
willing to fetch at all, fetch the server's ``.well-known/smart-configuration``
through ``FHIRProvider.discover()``, parse it, and read the endpoints and PKCE
signal an authorization request needs — against the real Epic and public discovery
documents, with HTTP mocked by respx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.providers import targets
from app.providers.discovery import (
    DiscoveryNotFoundError,
    DiscoveryParseError,
    DiscoveryUnreachableError,
    SMARTDiscovery,
)
from app.providers.targets import UnresolvedTarget, UnsafeTarget, ensure_fetchable

WELL_KNOWN = "/.well-known/smart-configuration"

# A public literal, so the refusal table needs no name resolution to run.
PUBLIC_IP_ISS = "https://8.8.8.8/fhir"

EPIC_ISS = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
PUBLIC_ISS = "https://launch.smarthealthit.org/v/r4/fhir"
CERNER_ISS = (
    "https://fhir-ehr-code.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d"
)


# Which issuers we are willing to fetch at all.
#
# Table-driven rather than flow-driven on purpose: this is one pure decision with
# many ways to get it wrong, and the cases below are the ones that would hurt. The
# journey it guards — a refused issuer answering without a request leaving the
# process — is driven end to end in tests/test_endpoint_check_flow.py.


@pytest.mark.parametrize(
    "iss",
    [
        pytest.param("file:///etc/passwd", id="not-http"),
        pytest.param("ftp://8.8.8.8/fhir", id="not-http-either"),
        pytest.param("8.8.8.8/fhir", id="no-scheme"),
        pytest.param("https:///fhir", id="no-host"),
        pytest.param("https://[::1/fhir", id="malformed-ipv6-literal"),
        pytest.param("https://8.8.8.8:99999/fhir", id="port-out-of-range"),
        pytest.param("https://user:pw@8.8.8.8/fhir", id="carries-credentials"),
        pytest.param("http://127.0.0.1:5432/fhir", id="loopback"),
        pytest.param("https://[::1]/fhir", id="loopback-v6"),
        pytest.param("http://10.1.2.3/fhir", id="rfc1918"),
        pytest.param("http://169.254.169.254/latest/meta-data", id="instance-metadata"),
        pytest.param("http://100.64.0.1/fhir", id="carrier-grade-nat"),
        pytest.param("https://[::ffff:10.0.0.1]/fhir", id="rfc1918-written-as-v6"),
        # A FHIR base ends before either of these, and the well-known path is
        # appended to whatever comes back: a query swallows it and a fragment
        # discards it, so both would answer about a document never fetched.
        pytest.param("https://8.8.8.8/r4?_format=json", id="carries-a-query"),
        pytest.param("https://8.8.8.8/r4#frag", id="carries-a-fragment"),
        pytest.param("https://8.8.8.8/r4?", id="empty-query"),
        # The IDNA codec refuses a label over 63 characters before any lookup, and
        # that is not a resolution failure. Uncaught it would be a 500.
        pytest.param("https://" + "a" * 64 + ".com/fhir", id="unencodable-hostname"),
    ],
)
async def test_an_issuer_we_should_not_fetch_is_refused(iss):
    with pytest.raises(UnsafeTarget):
        await ensure_fetchable(iss)


async def test_a_public_issuer_is_accepted_and_normalized():
    assert await ensure_fetchable(f"  {PUBLIC_IP_ISS}/  ") == PUBLIC_IP_ISS


async def test_a_non_default_port_is_allowed():
    """Not an oversight. ONC's list carries endpoints on ports like 9443, and
    refusing them to narrow a slow oracle anyone can run directly would cost real
    servers for very little."""
    iss = "https://8.8.8.8:9443/fhir-server/api/v4"

    assert await ensure_fetchable(iss + "/") == iss


async def test_a_hostname_pointing_into_private_space_is_refused(monkeypatch):
    """The case a scheme-only check misses: the URL looks ordinary, the name does not.

    Nothing about `https://fhir.internal.example/r4` reads as dangerous until it is
    resolved, which is why the guard resolves before deciding.
    """
    monkeypatch.setattr(targets, "_resolve", _resolving_to("10.0.0.7"))

    with pytest.raises(UnsafeTarget):
        await ensure_fetchable("https://fhir.internal.example/r4")


async def test_one_private_answer_among_public_ones_is_enough_to_refuse(monkeypatch):
    monkeypatch.setattr(targets, "_resolve", _resolving_to("93.184.216.34", "127.0.0.1"))

    with pytest.raises(UnsafeTarget):
        await ensure_fetchable("https://split-horizon.example/r4")


async def test_a_host_that_does_not_resolve_is_told_apart_from_one_we_refuse(monkeypatch):
    """Distinct exceptions because callers owe their users distinct answers.

    A name that no longer resolves is an honest "gone", not a bad request.
    """
    async def _nxdomain(host):
        raise UnresolvedTarget(f"{host} does not resolve")

    monkeypatch.setattr(targets, "_resolve", _nxdomain)

    with pytest.raises(UnresolvedTarget):
        await ensure_fetchable("https://never-existed.example/r4")


def _resolving_to(*addresses: str):
    async def _resolve(host: str) -> list[str]:
        return list(addresses)

    return _resolve


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


@respx.mock
async def test_a_cached_answer_reports_when_it_was_actually_fetched(epic_smart_config):
    """The timestamp has to survive the cache, or anything reporting it starts lying.

    A caller telling a user "checked just now" off a fifteen-minute-old cache entry
    reintroduces exactly the staleness such a check exists to replace.
    """
    route = respx.get(EPIC_ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=epic_smart_config)
    )
    discovery = SMARTDiscovery()

    first = await discovery.fetch_result(EPIC_ISS)
    second = await discovery.fetch_result(EPIC_ISS)

    assert route.call_count == 1, "the second call should have been served from cache"
    assert second.fetched_at == first.fetched_at


@respx.mock
async def test_the_cache_does_not_grow_without_bound(epic_smart_config):
    """An unbounded cache is a way to spend this process's memory, once the issuer
    reaching it is a caller's to choose rather than a provider allowlist's.

    Asserted through the network instead of the cache dict: what matters is that
    something was dropped and re-fetched, not how it was stored.
    """
    route = respx.get(url__regex=r"https://\w\.example/fhir/\.well-known/.*").mock(
        return_value=httpx.Response(200, json=epic_smart_config)
    )
    discovery = SMARTDiscovery(max_entries=3)

    for host in "abcde":
        await discovery.fetch_result(f"https://{host}.example/fhir")
    assert route.call_count == 5

    await discovery.fetch_result("https://e.example/fhir")
    await discovery.fetch_result("https://a.example/fhir")

    assert route.call_count == 6, (
        "the newest issuer should still be cached and the oldest re-fetched"
    )


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
