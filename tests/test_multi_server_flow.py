"""One adapter, many servers: the same flow driven against three real EHRs.

Epic, the public SMART launcher, and the Cerner/Oracle Health sandbox differ in
what they advertise — Epic is a confidential client authenticating with HTTP
Basic, the other two are public clients that lean on PKCE alone. None of that is
hardcoded: the endpoints run each server's real discovery document through the
same ``/auth/start`` and ``/auth/callback`` code, and PKCE turns on because the
document says ``S256``. Adding a server is a config row plus its discovery doc,
which is what this test exists to prove.

Discovery and the token endpoint are mocked with respx; the flow itself is real.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.auth.models import OAuthState
from tests.app_harness import (
    CERNER_SANDBOX,
    EPIC_SANDBOX,
    SMART_LAUNCHER,
    app_db as _app_db,
    client as _client,
    load_fixture,
)


# Each server the single adapter must drive. The descriptors live in the harness;
# what belongs here is the one fact this test turns on and the app does not
# discover: whether the registration carries a client secret (confidential) or
# not (public).
SERVERS = [
    pytest.param({**EPIC_SANDBOX, "confidential": True}, id="epic"),
    pytest.param({**SMART_LAUNCHER, "confidential": False}, id="smart-launcher"),
    pytest.param({**CERNER_SANDBOX, "confidential": False}, id="cerner"),
]


@respx.mock
@pytest.mark.parametrize("server", SERVERS)
async def test_same_adapter_completes_pkce_flow_for_each_server(
    server, tmp_path, epic_token_response
):
    iss = server["iss"]
    well_known = iss + "/.well-known/smart-configuration"
    respx.get(well_known).mock(
        return_value=httpx.Response(200, json=load_fixture(server["smart_config"]))
    )
    token_route = respx.post(server["token_url"]).mock(
        return_value=httpx.Response(200, json=epic_token_response)
    )

    url = f"sqlite+aiosqlite:///{tmp_path / (server['provider'] + '.db')}"
    async with _app_db(url) as factory:
        async with _client() as client:
            start = await client.get(
                "/auth/start",
                params={"provider": server["provider"], "iss": iss},
                follow_redirects=False,
            )
            assert start.status_code == 307
            location = start.headers["location"]
            query = parse_qs(urlparse(location).query)

            # The redirect is rooted at the server's *discovered* authorize
            # endpoint, with aud pinned to the issuer and S256 PKCE attached —
            # the same three facts, sourced from three different documents.
            assert location.startswith(server["authorize_url"])
            assert query["aud"] == [iss]
            assert query["code_challenge_method"] == ["S256"]
            assert "code_challenge" in query
            state = query["state"][0]

            # The per-authorization verifier is held server-side with the state.
            async with factory() as session:
                row = await session.get(OAuthState, state)
                assert row is not None and row.code_verifier is not None

            callback = await client.post(
                "/auth/callback", json={"code": "auth-code-123", "state": state}
            )
            assert callback.status_code == 200

    # The exchange replayed the stored verifier and authenticated the way this
    # server expects: HTTP Basic for the confidential client, PKCE-only (client
    # id in the body, no Authorization header) for the public ones.
    exchange = token_route.calls.last.request
    body = {k: v[0] for k, v in parse_qs(exchange.content.decode()).items()}
    assert body["code_verifier"]
    if server["confidential"]:
        assert exchange.headers["Authorization"].startswith("Basic ")
    else:
        assert "Authorization" not in exchange.headers
        assert body["client_id"]


# A standalone launch against the public SMART launcher encodes its launch
# context in the aud path (.../v/r4/sim/<opts>/fhir) rather than the plain base,
# so the launcher provider accepts any FHIR base under its host — while a
# look-alike host is still refused before any network call.
LAUNCHER_SIM_ISS = (
    "https://launch.smarthealthit.org/v/r4/sim/eyJsYXVuY2hfdHlwZSI6InBhdGllbnQifQ/fhir"
)


@respx.mock
async def test_launcher_accepts_standalone_sim_issuer_under_its_host(tmp_path):
    respx.get(LAUNCHER_SIM_ISS + "/.well-known/smart-configuration").mock(
        return_value=httpx.Response(
            200, json=load_fixture("smarthealthit_smart_config.json")
        )
    )
    url = f"sqlite+aiosqlite:///{tmp_path / 'launcher_sim.db'}"
    async with _app_db(url):
        async with _client() as client:
            response = await client.get(
                "/auth/start",
                params={"provider": "SMART_LAUNCHER", "iss": LAUNCHER_SIM_ISS},
                follow_redirects=False,
            )
    assert response.status_code == 307
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["aud"] == [LAUNCHER_SIM_ISS]


@respx.mock
async def test_launcher_prefix_does_not_allow_a_lookalike_host(tmp_path):
    # The prefix ends in the launcher host + "/v/r4/", so a suffix-style look-alike
    # host cannot match. @respx.mock with no registered routes makes any outbound
    # request raise, so this also proves the rejection happens before discovery.
    url = f"sqlite+aiosqlite:///{tmp_path / 'launcher_evil.db'}"
    async with _app_db(url):
        async with _client() as client:
            response = await client.get(
                "/auth/start",
                params={
                    "provider": "SMART_LAUNCHER",
                    "iss": "https://launch.smarthealthit.org.evil.example/v/r4/sim/x/fhir",
                },
                follow_redirects=False,
            )
    assert response.status_code == 400
    assert response.json()["error"] == "Issuer not allowed for this provider"
    assert not respx.calls  # no discovery request was attempted


def test_configured_issuer_prefixes_fully_commit_their_host():
    # The launcher fix's safety rests on every prefix terminating its authority
    # with a path separator; enforce that on the shipped config and reject unsafe
    # additions (a bare host, or a string mistakenly used in place of a list).
    from app.providers.config import EHR_CONFIGS, validate_issuer_prefixes

    validate_issuer_prefixes(EHR_CONFIGS)  # shipped config is safe

    with pytest.raises(ValueError):
        validate_issuer_prefixes(
            {"BAD": {"allowed_issuer_prefixes": ["https://launch.smarthealthit.org"]}}
        )
    with pytest.raises(ValueError):
        validate_issuer_prefixes(
            {"BAD": {"allowed_issuer_prefixes": "https://launch.smarthealthit.org/"}}
        )


def test_issuer_prefixes_are_rejected_on_a_provider_that_sends_a_secret():
    # Prefix matching is only safe for a public client: on a confidential client
    # it would let the secret reach any host under the prefix. The prefix below is
    # host-committing, so only the client_secret coupling can reject this.
    from app.providers.config import validate_issuer_prefixes

    with pytest.raises(ValueError):
        validate_issuer_prefixes(
            {
                "BAD": {
                    "client_secret": "shh",
                    "allowed_issuer_prefixes": ["https://fhir.example.org/r4/"],
                }
            }
        )
