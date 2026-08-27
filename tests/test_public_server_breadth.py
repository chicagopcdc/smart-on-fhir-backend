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

from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from pydantic import TypeAdapter, ValidationError

from app.providers.discovery import DiscoveryParseError, SMARTDiscovery
from app.providers.generic import GenericSMARTProvider, SMARTProviderError
from app.providers.models import SMARTConfiguration
from tests import upstream
from tests.app_harness import load_fixture, save_fixture

WELL_KNOWN = "/.well-known/smart-configuration"

CORPUS_FILE = "public_smart_configurations.json"
CORPUS = load_fixture(CORPUS_FILE)
USABLE = [pytest.param(s, id=s["id"]) for s in CORPUS["servers"] if s["usable"]]
REFUSED = [pytest.param(s, id=s["id"]) for s in CORPUS["servers"] if not s["usable"]]
SERVERS = USABLE + REFUSED


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


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


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
    assert _query(request.url)["aud"] == [iss.rstrip("/")]

    # PKCE follows what the server advertised, not what we would prefer.
    assert (request.code_verifier is not None) == config.supports_pkce
    assert ("code_challenge" in _query(request.url)) == config.supports_pkce


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

    # An exact value, not a substring: a partial decode would let this pass without
    # ever comparing the thing it is about.
    assert _query(request.url)["aud"] == [entry["source"].rstrip("/")]


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
        "advertises no PKCE": any(not c.supports_pkce for c in usable),
        "advertises no auth methods": any(
            not c.token_endpoint_auth_methods_supported for c in usable
        ),
        "advertises only asymmetric auth": any(
            c.token_endpoint_auth_methods_supported
            and not symmetric & set(c.token_endpoint_auth_methods_supported)
            for c in usable
        ),
        "publishes no revocation endpoint": any(
            c.revocation_endpoint is None for c in usable
        ),
        "publishes no issuer": any(c.issuer is None for c in usable),
    }

    missing = [shape for shape, found in shapes.items() if not found]
    assert not missing, f"no server in the corpus {' or '.join(missing)}"

    # And the two that are refused for different reasons are both still there.
    refused = {s["id"] for s in CORPUS["servers"] if not s["usable"]}
    assert len(refused) >= 3, "the corpus should keep some documents we refuse"


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("jwks_uri", "/well-known/jwks.json", id="relative-path"),
        pytest.param("management_endpoint", "", id="empty-string"),
        pytest.param("response_types_supported", None, id="null-list"),
        pytest.param("scopes_supported", [["openid"]], id="list-in-a-list"),
        pytest.param("issuer", "urn:oid:1.2.3", id="not-a-url"),
    ],
)
def test_one_unreadable_optional_field_does_not_cost_the_whole_document(field, value):
    """Every way an optional field has been seen published wrong, in one table.

    Written against a synthetic document rather than a captured one on purpose: no
    server in the corpus pairs *good* authorize and token endpoints with a relative
    optional URL, so the corpus alone cannot hold that case down. Carepaths comes
    closest and is refused anyway, for endpoints that are relative too.

    All five of these mean one thing — the server described itself badly in a field
    nothing reads — so all five have to land on the same side of the line.
    """
    document = {
        "authorization_endpoint": "https://ok.example/authorize",
        "token_endpoint": "https://ok.example/token",
        field: value,
    }

    config = SMARTConfiguration.model_validate(document)

    assert str(config.authorization_endpoint) == "https://ok.example/authorize"
    # Dropped to the field's default, which is the state a server that never
    # mentioned it would leave us in.
    assert getattr(config, field) == SMARTConfiguration.model_fields[field].get_default(
        call_default_factory=True
    )


@pytest.mark.parametrize(
    "field,sent,kept",
    [
        pytest.param(
            "code_challenge_methods_supported", ["S256", None], ["S256"], id="pkce"
        ),
        pytest.param(
            "token_endpoint_auth_methods_supported",
            ["private_key_jwt", 42],
            ["private_key_jwt"],
            id="auth-methods",
        ),
        pytest.param("scopes_supported", ["openid", None, "fhirUser"],
                     ["openid", "fhirUser"], id="scopes"),
    ],
)
def test_one_bad_item_does_not_discard_the_readable_rest_of_a_list(field, sent, kept):
    """Both fields here decide behaviour, so emptying them fails quietly.

    An empty `code_challenge_methods_supported` is indistinguishable from a server
    that advertises no PKCE, and an empty `token_endpoint_auth_methods_supported`
    is the one case that falls back to sending a client secret — which is exactly
    what a server advertising only `private_key_jwt` must never be sent. Dropping
    the whole list on one bad item turns a document we would have refused outright
    into a silent downgrade.
    """
    config = SMARTConfiguration.model_validate(
        {
            "authorization_endpoint": "https://ok.example/authorize",
            "token_endpoint": "https://ok.example/token",
            field: sent,
        }
    )

    assert getattr(config, field) == kept


def test_the_asymmetric_only_guard_survives_a_malformed_neighbour():
    """The consequence of the above, at the layer that acts on it.

    A stray item alongside `private_key_jwt` must not turn the refusal into a
    client_secret_basic request.
    """
    config = SMARTConfiguration.model_validate(
        {
            "authorization_endpoint": "https://ok.example/authorize",
            "token_endpoint": "https://ok.example/token",
            "token_endpoint_auth_methods_supported": ["private_key_jwt", 42],
        }
    )

    with pytest.raises(SMARTProviderError):
        _provider(client_secret="a-secret")._client_authentication(config)


def test_a_required_endpoint_is_never_treated_that_way():
    """The other half of the rule. These two are acted on, so a value that cannot
    be read is a refusal rather than something to drop and carry on without."""
    for bad in ("/login", "", None, "urn:oid:1.2.3", "javascript:alert(1)"):
        with pytest.raises(ValidationError):
            SMARTConfiguration.model_validate(
                {
                    "authorization_endpoint": bad,
                    "token_endpoint": "https://ok.example/token",
                }
            )


def test_the_entries_kept_for_malformed_metadata_still_carry_some():
    """The regression entries have to keep the junk they were captured for.

    Their whole job is to fail if the tolerance in `SMARTConfiguration` is removed,
    and they can only do that while they still publish something malformed. A
    routine re-capture from a server that has since fixed its document would leave
    the validator uncovered with the suite fully green, so this asserts the raw
    entry rather than the parsed one — the parsed one has already been cleaned up.
    """
    for server in CORPUS["servers"]:
        if server["kind"] != "regression":
            continue

        junk = {
            name: value
            for name, value in server["configuration"].items()
            if (field := SMARTConfiguration.model_fields.get(name))
            and not field.is_required()
            and not _parses_as(field.annotation, value)
        }
        assert junk, (
            f"{server['id']} no longer publishes anything malformed, so it no "
            "longer covers the tolerance it was captured for"
        )


def _parses_as(annotation, value) -> bool:
    """Whether a raw value would satisfy its field on its own terms.

    Asked of the annotation directly rather than through the model, because the
    model is what applies the leniency being tested for — run through it, every
    value looks fine.
    """
    try:
        TypeAdapter(annotation).validate_python(value)
    except ValidationError:
        return False
    return True


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


@pytest.fixture(scope="session")
def corpus_recapture(request):
    """Collect refreshed entries and write the manifest once, at the end.

    The check below is parametrized sixteen ways over a single file, so writing
    per entry would rewrite the whole manifest sixteen times. Collecting also
    keeps the date honest: `capturedAt` moves only when every server answered in
    this run, so a run where three were unreachable leaves a date that still
    describes the document rather than claiming a sweep that did not happen.
    """
    if not request.config.getoption("--refresh-fixtures"):
        yield lambda server_id, configuration: None
        return

    refreshed: dict[str, dict] = {}

    def record(server_id: str, configuration: dict) -> None:
        refreshed[server_id] = configuration

    yield record

    if not refreshed:
        return

    manifest = load_fixture(CORPUS_FILE)
    for entry in manifest["servers"]:
        if entry["id"] in refreshed:
            entry["configuration"] = refreshed[entry["id"]]
    if len(refreshed) == len(manifest["servers"]):
        manifest["capturedAt"] = date.today().isoformat()
    save_fixture(CORPUS_FILE, manifest)


@pytest.mark.live
@pytest.mark.parametrize("server", SERVERS)
async def test_live_each_server_still_publishes_what_was_captured(
    server, corpus_recapture
):
    name = server["label"]

    with upstream.reaching(name):
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
            response = await client.get(
                server["source"].rstrip("/") + WELL_KNOWN,
                headers={"Accept": "application/json"},
            )
    upstream.served(name, response)

    # A vendor here is one this backend does not hold credentials for and cannot
    # authorize against, so it is free to refuse an anonymous reader without that
    # being a change we need to act on. The rule is stricter for the issuers we do
    # authorize against, in test_smart_discovery_flow.py, where a refusal means the
    # login this backend offers has quietly stopped being available.
    if response.status_code == 403:
        pytest.skip(f"{name} refuses an unauthenticated configuration request")

    assert response.status_code == 200, response.text
    live = response.json()

    # `server["configuration"]` was read at import, so this compares against what
    # was on disk when the run started even on a refresh run.
    corpus_recapture(server["id"], live)

    assert sorted(live) == sorted(server["configuration"]), (
        f"{name} changed which fields it publishes"
    )
