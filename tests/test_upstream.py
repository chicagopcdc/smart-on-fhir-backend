"""Which upstream failures the live suite shouts about, decided offline.

The live suite's whole value rests on one distinction: a server that changed what
it publishes is news, a server that is not answering is weather. Left to a real
server to demonstrate, that rule would only ever be exercised on the rare day
something was actually broken, and it would be wrong for a while before anyone
noticed. So the rule is pinned here instead, against every failure a server can
hand back, with the network mocked.

Read the two tables as the rule itself. What separates them is not severity but
whether anything came back at all: a 503 is a server admitting it cannot answer
right now, while a 404 is a server answering that the thing we have been reading
for months is not there any more.
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
from tests import upstream

WELL_KNOWN = "/.well-known/smart-configuration"
ISS = "https://fhir.example-hospital.org/R4"
NAME = "Example Hospital"


def _answering(reply):
    """Point the issuer's well-known URL at a response or a transport failure."""
    route = respx.get(ISS + WELL_KNOWN)
    if isinstance(reply, httpx.Response):
        return route.mock(return_value=reply)
    return route.mock(side_effect=reply)


# --- weather: the server never really answered -------------------------------

OUTAGES = [
    pytest.param(httpx.Response(500), id="500"),
    pytest.param(httpx.Response(503), id="503-unavailable"),
    pytest.param(httpx.Response(504), id="504-gateway-timeout"),
    pytest.param(httpx.Response(429), id="429-throttled"),
    pytest.param(httpx.Response(408), id="408-request-timeout"),
    pytest.param(httpx.ConnectError("no route to host"), id="connect-error"),
    pytest.param(httpx.ConnectTimeout("timed out"), id="connect-timeout"),
    pytest.param(httpx.ReadTimeout("timed out"), id="read-timeout"),
    pytest.param(httpx.RemoteProtocolError("peer closed"), id="connection-dropped"),
]


@respx.mock
@pytest.mark.parametrize("reply", OUTAGES)
async def test_a_server_that_did_not_answer_skips_and_says_which_one(reply):
    _answering(reply)

    with pytest.raises(pytest.skip.Exception) as skipped:
        with upstream.reaching(NAME):
            await SMARTDiscovery().fetch(ISS)

    # Naming the server is the point: a skip nobody can attribute is a skip
    # nobody can act on when it turns out to be permanent.
    assert NAME in str(skipped.value)


# --- news: the server answered, and it was not what we had -------------------

CHANGES = [
    pytest.param(httpx.Response(404), DiscoveryNotFoundError, id="404-gone"),
    pytest.param(httpx.Response(403), DiscoveryUnreachableError, id="403-now-refused"),
    pytest.param(httpx.Response(401), DiscoveryUnreachableError, id="401-now-guarded"),
    pytest.param(
        httpx.Response(200, text="<html>not json</html>"),
        DiscoveryParseError,
        id="stopped-serving-json",
    ),
    pytest.param(
        httpx.Response(200, json={"token_endpoint": "https://ehr.example/token"}),
        DiscoveryParseError,
        id="dropped-the-authorize-endpoint",
    ),
]


@respx.mock
@pytest.mark.parametrize("reply,expected", CHANGES)
async def test_a_server_that_answered_differently_fails_with_its_own_error(
    reply, expected
):
    """Including the two the app itself calls unreachable.

    A 401 or a 403 is named for what it costs the application, which cannot use
    the endpoint either way. Here the question is a different one, and the answer
    is not in doubt: something replied, so the endpoint has not gone away, it has
    started refusing us. Re-running will not fix that.
    """
    _answering(reply)

    # The skip is caught rather than left to propagate. A skip that escapes skips
    # the test, so a misclassified change would read as a green run with one quiet
    # skip in it, which is the failure this whole file exists to prevent happening
    # inside the file's own assertions.
    try:
        with pytest.raises(expected):
            with upstream.reaching(NAME):
                await SMARTDiscovery().fetch(ISS)
    except pytest.skip.Exception as skipped:
        pytest.fail(f"a server that answered was reported as an outage: {skipped}")


# --- the same rule, for a test holding a raw response ------------------------


def test_an_unwell_raw_response_skips():
    with pytest.raises(pytest.skip.Exception) as skipped:
        upstream.served(NAME, httpx.Response(502))

    assert NAME in str(skipped.value)
    assert "502" in str(skipped.value)


def test_a_healthy_raw_response_comes_back_untouched():
    response = httpx.Response(200, json={"ok": True})

    assert upstream.served(NAME, response) is response


def test_a_refusal_is_handed_back_for_the_test_to_judge():
    # 403 is the one status the corpus test tolerates on its own terms, so the
    # helper must not decide it here.
    refused = httpx.Response(403)

    assert upstream.served(NAME, refused) is refused


# --- the mechanism the rule is built on --------------------------------------


@respx.mock
async def test_the_typed_discovery_errors_still_carry_what_caused_them():
    """The classifier reads ``__cause__`` to tell the two apart.

    Nothing else in the suite would notice if discovery stopped chaining its
    errors, and the failure would be quiet in the worst way: every outage would
    start reading as a change, the live suite would go red on a bad network day,
    and the tables above would still pass, because they go through the same
    broken path.
    """
    _answering(httpx.Response(503))
    with pytest.raises(DiscoveryUnreachableError) as from_status:
        await SMARTDiscovery().fetch(ISS)

    respx.reset()
    _answering(httpx.ConnectError("no route to host"))
    with pytest.raises(DiscoveryUnreachableError) as from_transport:
        await SMARTDiscovery().fetch(ISS)

    assert isinstance(from_status.value.__cause__, httpx.HTTPStatusError)
    assert from_status.value.__cause__.response.status_code == 503
    assert isinstance(from_transport.value.__cause__, httpx.ConnectError)


# --- the same rule, for a raiser that only writes the status down -------------


def test_a_status_written_into_a_message_is_read_back_out():
    # The endpoint-list resolver formats the status into its message and chains
    # nothing, so the status has to be recovered from the text or not at all.
    assert upstream.status_reported_by(RuntimeError("listing 'x' returned 503")) == 503
    assert upstream.status_reported_by(RuntimeError("no CSV in the mirror")) is None


@pytest.mark.parametrize(
    "status,weather",
    [
        pytest.param(503, True, id="503-unavailable"),
        pytest.param(502, True, id="502-bad-gateway"),
        pytest.param(429, True, id="429-throttled"),
        pytest.param(408, True, id="408-request-timeout"),
        pytest.param(404, False, id="404-gone"),
        pytest.param(403, False, id="403-refused"),
        pytest.param(200, False, id="200-fine"),
    ],
)
def test_the_statuses_that_mean_ask_again_later_are_the_only_ones(status, weather):
    """Pinned as a table because the set was once written out twice.

    A second copy spelled the rule as two literal codes, which quietly left every
    5xx on the wrong side of it — the most likely upstream failure of all,
    reported as though the server had changed.
    """
    assert upstream.is_outage(status) is weather
