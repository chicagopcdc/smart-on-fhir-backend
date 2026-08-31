"""A refusal that names one thing to the caller names seven to the log.

`POST /auth/connect` and `POST /auth/callback` flatten every way a server can
fail into two sentences, on purpose: the wording that helps a patient is not the
wording that helps whoever has to fix it, and telling a browser which of them it
was would tell anyone probing issuers what they found.

The cost was that the distinction stopped existing anywhere. A connect that
failed answered "Could not read the server's SMART configuration" and left
nothing behind saying whether the server had published no configuration,
published a broken one, refused the request, or never answered — four different
problems with four different answers, one of which is "wait and try again" and
one of which is "this endpoint is wrong".

Each case here drives the real flow, checks the caller still sees exactly what
they saw before, and then checks the log says which one it was. Both halves
matter: a change that improved the log by changing the response would pass on the
half that is easy to notice and fail the users of the half that is not.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.app_harness import (
    SMART_LAUNCHER as LAUNCHER,
    app_db,
    client,
    load_fixture,
)

WELL_KNOWN = LAUNCHER["iss"] + "/.well-known/smart-configuration"

CONFIG_UNREADABLE = "Could not read the server's SMART configuration"
EXCHANGE_FAILED = "Token exchange failed"


@pytest.fixture
def logged(caplog):
    caplog.set_level("INFO", logger="app.api.auth")
    return caplog


async def _connect(http):
    return await http.post(
        "/auth/connect",
        json={"provider": LAUNCHER["provider"], "iss": LAUNCHER["iss"]},
    )


def _fields(caplog, event: str) -> dict:
    """The structured keys of the one record for this event."""
    matching = [
        record.fields
        for record in caplog.records
        if getattr(record, "fields", {}).get("event") == event
    ]
    assert matching, f"nothing logged {event}; got {[r.getMessage() for r in caplog.records]}"
    assert len(matching) == 1, f"{event} was logged {len(matching)} times"
    return matching[0]


# --- discovery: four failures behind one sentence -----------------------------


@pytest.mark.parametrize(
    ("name", "answer", "reason", "status"),
    [
        (
            "publishes nothing",
            httpx.Response(404, text="not found"),
            "no_smart_configuration",
            404,
        ),
        (
            "refuses an unauthenticated read",
            httpx.Response(403, text="forbidden"),
            "unreachable",
            403,
        ),
        (
            "publishes something unusable",
            httpx.Response(200, json={"authorization_endpoint": "not-a-url"}),
            "invalid_smart_configuration",
            None,
        ),
        (
            "never answers",
            httpx.ConnectTimeout("timed out"),
            "unreachable",
            None,
        ),
    ],
)
@respx.mock
async def test_a_discovery_failure_is_named_in_the_log(
    name, answer, reason, status, logged, tmp_path
):
    if isinstance(answer, Exception):
        respx.get(WELL_KNOWN).mock(side_effect=answer)
    else:
        respx.get(WELL_KNOWN).mock(return_value=answer)

    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'discovery.db'}"):
        async with client() as http:
            response = await _connect(http)

    # Unchanged, and the point of the exercise: four servers, one answer.
    assert response.status_code == 502, name
    assert response.json() == {"detail": CONFIG_UNREADABLE}, name

    entry = _fields(logged, "auth.discovery.failed")
    assert entry["reason"] == reason, name
    assert entry["status"] == status, name
    assert entry["provider"] == LAUNCHER["provider"]
    assert entry["iss"] == LAUNCHER["iss"]


@respx.mock
async def test_a_refused_discovery_is_told_from_one_that_never_answered(logged, tmp_path):
    """The two DiscoveryUnreachableError covers, which only the status separates.

    A server that answered 403 has made a decision — the endpoint needs
    registration, or is not meant to be reached this way. A connection that was
    never made is weather. Retrying helps with exactly one of them, and before
    this the log offered no way to tell which you had.
    """
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'unreachable.db'}"):
        async with client() as http:
            respx.get(WELL_KNOWN).mock(return_value=httpx.Response(403))
            refused = await _connect(http)
            refused_entry = _fields(logged, "auth.discovery.failed")

            logged.clear()
            respx.get(WELL_KNOWN).mock(side_effect=httpx.ConnectError("no route"))
            silent = await _connect(http)
            silent_entry = _fields(logged, "auth.discovery.failed")

    assert refused.status_code == silent.status_code == 502
    assert refused.json() == silent.json(), "the caller cannot tell these apart"
    assert refused_entry["reason"] == silent_entry["reason"] == "unreachable"
    assert refused_entry["status"] == 403
    assert silent_entry["status"] is None, "a connection never made has no status"


# --- the exchange -------------------------------------------------------------


async def _reach_callback(http, token_route):
    """Start an authorization, then answer its token endpoint with ``token_route``."""
    respx.get(WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=load_fixture(LAUNCHER["smart_config"]))
    )
    started = await http.post(
        "/auth/connect",
        json={"provider": LAUNCHER["provider"], "iss": LAUNCHER["iss"]},
    )
    assert started.status_code == 200, started.text

    route = respx.post(LAUNCHER["token_url"])
    if isinstance(token_route, Exception):
        route.mock(side_effect=token_route)
    else:
        route.mock(return_value=token_route)

    return await http.post(
        "/auth/callback",
        json={"code": "test-auth-code", "state": started.json()["state"]},
    )


@pytest.mark.parametrize(
    ("name", "answer", "expected_status", "reason", "status"),
    [
        (
            "the grant is gone",
            httpx.Response(400, json={"error": "invalid_grant"}),
            400,
            "invalid_grant",
            400,
        ),
        (
            "the client is not registered",
            httpx.Response(401, json={"error": "invalid_client"}),
            400,
            "invalid_client",
            401,
        ),
        (
            "the server refuses without saying why",
            httpx.Response(500, text="upstream is unwell"),
            400,
            "rejected",
            500,
        ),
        (
            "the server never answers",
            httpx.ReadTimeout("timed out"),
            502,
            "unreachable",
            None,
        ),
    ],
)
@respx.mock
async def test_an_exchange_failure_is_named_in_the_log(
    name, answer, expected_status, reason, status, logged, tmp_path
):
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'exchange.db'}"):
        async with client() as http:
            response = await _reach_callback(http, answer)

    # One sentence for every one of them, under two different statuses. That is
    # the flattening this exists to make survivable, not to undo.
    assert response.status_code == expected_status, name
    assert response.json() == {"detail": EXCHANGE_FAILED}, name

    entry = _fields(logged, "auth.token_exchange.failed")
    assert entry["reason"] == reason, name
    assert entry["status"] == status, name
    assert entry["provider"] == LAUNCHER["provider"]


@respx.mock
async def test_a_refusal_and_an_outage_share_a_sentence_and_not_a_log_line(
    logged, tmp_path
):
    """Both say "Token exchange failed"; only one of them is our problem to chase."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'both.db'}"):
        async with client() as http:
            refused = await _reach_callback(
                http, httpx.Response(400, json={"error": "invalid_grant"})
            )
            refused_entry = _fields(logged, "auth.token_exchange.failed")

            logged.clear()
            unreachable = await _reach_callback(http, httpx.ConnectError("down"))
            unreachable_entry = _fields(logged, "auth.token_exchange.failed")

    assert refused.json() == unreachable.json() == {"detail": EXCHANGE_FAILED}
    assert refused_entry["reason"] == "invalid_grant"
    assert unreachable_entry["reason"] == "unreachable"


@respx.mock
async def test_nothing_of_the_exchange_itself_reaches_the_log(logged, tmp_path):
    """The code and the state are credentials, and a failure is when they leak."""
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'quiet.db'}"):
        async with client() as http:
            respx.get(WELL_KNOWN).mock(
                return_value=httpx.Response(200, json=load_fixture(LAUNCHER["smart_config"]))
            )
            started = await http.post(
                "/auth/connect",
                json={"provider": LAUNCHER["provider"], "iss": LAUNCHER["iss"]},
            )
            state = started.json()["state"]
            respx.post(LAUNCHER["token_url"]).mock(
                return_value=httpx.Response(400, json={"error": "invalid_grant"})
            )
            await http.post(
                "/auth/callback",
                json={"code": "a-real-looking-authorization-code", "state": state},
            )

    text = logged.text
    assert "a-real-looking-authorization-code" not in text
    assert state not in text
