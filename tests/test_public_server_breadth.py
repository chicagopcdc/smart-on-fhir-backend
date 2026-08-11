"""One adapter against sixteen public SMART servers.

The claim the provider layer rests on is that a server needs configuration and
credentials, never code: endpoints are discovered at runtime, so a new server
should be an entry in a table rather than a subclass. This is where that claim is
tested against documents real servers actually publish, captured from ONC's own
national list and from public sandboxes rather than written to suit the adapter.

Everything a server contributes lives in the captured manifest under
`tests/fixtures/`. Adding one means adding an entry there. Where an entry exists
because it exercises a particular branch, a named test below says which and why —
the corpus is only evidence if its entries differ, so the last test in the file
fails if the shapes that matter stop being represented.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.providers.discovery import DiscoveryParseError, SMARTDiscovery
from app.providers.generic import GenericSMARTProvider, SMARTProviderError
from app.providers.models import SMARTConfiguration
from tests.app_harness import load_fixture

WELL_KNOWN = "/.well-known/smart-configuration"

CORPUS = load_fixture("public_smart_configurations.json")
SERVERS = [
    pytest.param(server, id=server["id"]) for server in CORPUS["servers"]
]
USABLE = [param for param in SERVERS if param.values[0]["usable"]]
REFUSED = [param for param in SERVERS if not param.values[0]["usable"]]


def _provider(**overrides) -> GenericSMARTProvider:
    return GenericSMARTProvider(
        **{
            "client_id": "corpus-client",
            "redirect_uri": "http://localhost:3000/auth/callback",
            "aud": "https://ignored.example/fhir",
            "discovery": SMARTDiscovery(),
            **overrides,
        }
    )


def _entry(server_id: str) -> dict:
    return next(s for s in CORPUS["servers"] if s["id"] == server_id)


def _configuration(server_id: str) -> SMARTConfiguration:
    return SMARTConfiguration.model_validate(_entry(server_id)["configuration"])


# --- every server, through the one adapter ------------------------------------


@respx.mock
@pytest.mark.parametrize("server", USABLE)
async def test_the_shared_adapter_discovers_each_server_and_builds_its_auth_url(server):
    """The whole point, once per server: discovery through the unmodified adapter,
    then an authorization URL rooted at whatever that server said."""
    iss = server["source"]
    route = respx.get(iss.rstrip("/") + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=server["configuration"])
    )

    provider = _provider(aud=iss)
    config = await provider.discover(iss)
    request = provider.build_auth_url(config, state="corpus", scopes=["openid"])

    assert route.called
    assert request.url.startswith(str(config.authorization_endpoint))
    assert f"aud={iss.rstrip('/')}" in request.url.replace("%3A", ":").replace("%2F", "/")

    # PKCE follows what the server advertised, not what we would prefer.
    assert (request.code_verifier is not None) == config.supports_pkce
    assert ("code_challenge=" in request.url) == config.supports_pkce


@respx.mock
@pytest.mark.parametrize("server", REFUSED)
async def test_a_document_that_cannot_be_used_is_still_refused(server):
    """Tolerance for malformed description must not have become tolerance generally.

    Each of these is reachable and answers with JSON. None of them says where to
    send a user, so there is nothing to be lenient about.
    """
    iss = server["source"]
    respx.get(iss.rstrip("/") + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=server["configuration"])
    )

    with pytest.raises(DiscoveryParseError):
        await _provider(aud=iss).discover(iss)


# --- the branches particular servers are here for ----------------------------


def test_a_server_offering_only_asymmetric_auth_is_not_sent_a_secret():
    """ONC's own reference server advertises private_key_jwt alone.

    Sending Basic anyway would be a request the server rejects, reported as a
    failed token exchange rather than as the misconfiguration it is.
    """
    config = _configuration("inferno")
    assert config.token_endpoint_auth_methods_supported == ["private_key_jwt"]

    with pytest.raises(SMARTProviderError):
        _provider(client_secret="a-secret")._client_authentication(config)

    # A public client is unaffected: the client_id identifies it and PKCE does the
    # rest, which is how this backend registers against such a server.
    auth, fields = _provider()._client_authentication(config)
    assert auth is None
    assert fields == {"client_id": "corpus-client"}


def test_a_server_advertising_no_pkce_is_not_sent_a_challenge():
    config = _configuration("interop_community")
    assert config.supports_pkce is False

    request = _provider().build_auth_url(config, state="s", scopes=["openid"])

    assert request.code_verifier is None
    assert "code_challenge" not in request.url


def test_a_key_named_without_its_supported_suffix_is_ignored_not_misread():
    """interop.community publishes `token_endpoint_auth_methods`, which is not the
    field name. Reading it would be guessing at what a server meant."""
    raw = _entry("interop_community")["configuration"]
    assert "token_endpoint_auth_methods" in raw

    assert _configuration("interop_community").token_endpoint_auth_methods_supported == []


def test_a_server_advertising_no_auth_methods_falls_back_to_basic():
    config = _configuration("cms_blue_button")
    assert config.token_endpoint_auth_methods_supported == []

    auth, fields = _provider(client_secret="a-secret")._client_authentication(config)

    assert auth == ("corpus-client", "a-secret")
    assert fields == {}


@respx.mock
async def test_a_server_with_no_revocation_endpoint_is_not_asked_to_revoke():
    config = _configuration("medplum")
    assert config.revocation_endpoint is None

    revoked = await _provider().revoke_token(config, "t", token_type_hint="refresh_token")

    assert revoked is False
    assert not respx.calls, "there was no endpoint to post to"


def test_the_token_is_bound_to_the_issuer_we_were_given_not_the_one_discovered():
    """athenahealth names an issuer on a different host from its FHIR base.

    aud has to stay the FHIR server the token will be spent at, or the binding
    stops meaning anything.
    """
    entry = _entry("athenahealth_preview")
    config = _configuration("athenahealth_preview")
    assert str(config.issuer).startswith("https://athena.okta.com/")

    request = _provider(aud=entry["source"]).build_auth_url(config, "s", ["openid"])

    assert "athena.okta.com" not in request.url.replace("%3A", ":").replace("%2F", "/")


# --- keeping the corpus honest -----------------------------------------------


def test_the_corpus_still_covers_the_shapes_that_matter():
    """Sixteen lookalike servers would prove nothing, so the spread is asserted.

    If this fails, entries have been added or replaced until some shape the adapter
    has to handle stopped being represented — the message names which.
    """
    usable = [
        SMARTConfiguration.model_validate(s["configuration"])
        for s in CORPUS["servers"]
        if s["usable"]
    ]
    symmetric = {"client_secret_basic", "client_secret_post"}
    shapes = {
        "advertises no PKCE": [c for c in usable if not c.supports_pkce],
        "advertises no auth methods": [
            c for c in usable if not c.token_endpoint_auth_methods_supported
        ],
        "advertises only asymmetric auth": [
            c
            for c in usable
            if c.token_endpoint_auth_methods_supported
            and not symmetric & set(c.token_endpoint_auth_methods_supported)
        ],
        "publishes no revocation endpoint": [
            c for c in usable if c.revocation_endpoint is None
        ],
        "publishes no issuer": [c for c in usable if c.issuer is None],
    }

    missing = [shape for shape, found in shapes.items() if not found]
    assert not missing, f"no server in the corpus {' or '.join(missing)}"

    # And the two that are refused for different reasons are both still there.
    refused = {s["id"] for s in CORPUS["servers"] if not s["usable"]}
    assert len(refused) >= 3, "the corpus should keep some documents we refuse"


def test_every_entry_says_where_it_came_from_and_why_it_is_here():
    """A capture with no source cannot be re-verified, and one with no note becomes
    padding the next person cannot safely remove."""
    for server in CORPUS["servers"]:
        assert server["source"].startswith("https://"), server["id"]
        assert len(server["note"]) > 40, server["id"]
        assert server["kind"] in {"regression", "refused", "branch", "production"}


# --- opt-in: re-check the captures against the real servers.
# Run with `pytest -m live`. A failure here is news, not a broken test: it means a
# server changed what it publishes, and the entry needs re-capturing.


@pytest.mark.live
@pytest.mark.parametrize("server", SERVERS)
async def test_live_each_server_still_publishes_what_was_captured(server):
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
        response = await client.get(
            server["source"].rstrip("/") + WELL_KNOWN,
            headers={"Accept": "application/json"},
        )

    if response.status_code == 403:
        pytest.skip("this vendor refuses an unauthenticated configuration request")

    assert response.status_code == 200, response.text
    live = response.json()
    assert sorted(live) == sorted(server["configuration"]), (
        "the server changed which fields it publishes"
    )
