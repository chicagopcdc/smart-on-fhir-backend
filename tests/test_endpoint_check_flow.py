"""`/providers/endpoint-check`: asking an endpoint whether it can be used, now.

The flag on a search row is ONC's, recorded whenever it last probed, and the
published file it comes from can be months old. So an endpoint that has moved, let
its certificate lapse, or stopped publishing a SMART configuration still reads as
connectable, and the user finds out when the login screen never arrives.

These tests drive the four answers a caller can act on through the real route, and
the two things that make it safe to hand a URL: that a refused issuer costs no
outbound request, and that nothing an endpoint said is repeated back verbatim.

Discovery is served by respx; everything between it and the response is the real
application.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app import main
from app.api import deps
from app.core.config import get_settings
from app.providers import lantern, registry, targets

WELL_KNOWN = "/.well-known/smart-configuration"

ISS = "https://fhir.example-hospital.org/R4"

# The guard resolves before it decides, and respx intercepts the client rather
# than the resolver, so the fixture hostnames need an answer from somewhere.
PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def _resolve_to_a_public_address(request, monkeypatch):
    if "live" in request.keywords:
        return  # the live test resolves real hostnames, which is the point of it

    async def _resolve(host: str) -> list[str]:
        return [PUBLIC_ADDRESS]

    monkeypatch.setattr(targets, "_resolve", _resolve)


@pytest.fixture(autouse=True)
def _reset_lantern_state():
    lantern.reset_state()
    yield
    lantern.reset_state()


@pytest.fixture
def low_check_rate_limit(monkeypatch):
    """Turn the limiter on with a tiny budget for one test, then restore it.

    Mirrors `low_auth_rate_limit` in tests/test_security.py: the suite runs with
    throttling off, so an enabled limit has to be scoped to the test that wants it.
    """
    monkeypatch.setenv("ENDPOINT_CHECK_RATE_LIMIT", "2/minute")
    get_settings.cache_clear()
    deps.limiter.enabled = True
    deps.limiter.reset()
    yield
    deps.limiter.enabled = False
    deps.limiter.reset()
    get_settings.cache_clear()


async def _check(iss: str = ISS):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        return await client.get("/providers/endpoint-check", params={"iss": iss})


# The published-file plumbing, in the smallest form that yields one dated row.
FILE_DATE = "2025-11-14"
_CONTENTS = "https://api.github.com/repos/onc-healthit/onc-open-data/contents"
_RAW = "https://raw.githubusercontent.com/onc-healthit/onc-open-data/main"
_MONTH = "lantern-daily-data/2025/November"
_FILE = "11_14_2025endpointdata.csv"


def _mock_published_file() -> None:
    for path, entries in (
        ("lantern-daily-data", [("2025", "dir")]),
        ("lantern-daily-data/2025", [("November", "dir")]),
        (_MONTH, [(_FILE, "file")]),
    ):
        respx.get(f"{_CONTENTS}/{path}?ref=main").mock(
            return_value=httpx.Response(
                200, json=[{"name": name, "type": kind} for name, kind in entries]
            )
        )
    respx.get(f"{_RAW}/{_MONTH}/{_FILE}").mock(
        return_value=httpx.Response(
            200,
            text=(
                '"url","api_information_source_name","certified_api_developer_name",'
                '"capability_fhir_version","smart_http_response"\n'
                f'"{ISS}","Example Hospital","Epic Systems Corporation","4.0.1","200"\n'
            ),
        )
    )


# --- the four answers --------------------------------------------------------


@respx.mock
async def test_a_usable_endpoint_answers_with_where_to_send_the_user(public_smart_config):
    respx.get(ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=public_smart_config)
    )

    response = await _check()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["reachable"] is True
    assert body["smartCapable"] is True
    assert body["authorizationEndpoint"] == public_smart_config["authorization_endpoint"]
    assert body["tokenEndpoint"] == public_smart_config["token_endpoint"]
    assert body["iss"] == ISS


@respx.mock
async def test_an_endpoint_that_does_not_do_smart_is_still_reachable():
    """The distinction the whole endpoint exists for.

    A 404 means the server is healthy and simply does not do SMART — a settled no.
    Reporting that as unreachable would invite a caller to retry it forever.
    """
    respx.get(ISS + WELL_KNOWN).mock(return_value=httpx.Response(404))

    body = (await _check()).json()

    assert body["status"] == "no_smart_configuration"
    assert body["reachable"] is True, "a server that answered 404 answered"
    assert body["smartCapable"] is False
    assert body["authorizationEndpoint"] is None


@respx.mock
async def test_an_endpoint_publishing_something_unusable_is_told_apart():
    # Reachable, and it published a document — just not one that says where to
    # authorize, so there is nothing to send the user to.
    respx.get(ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json={"issuer": "https://example-hospital.org"})
    )

    body = (await _check()).json()

    assert body["status"] == "invalid_smart_configuration"
    assert body["reachable"] is True
    assert body["smartCapable"] is False


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"side_effect": httpx.ConnectError("no route")}, id="no-route"),
        pytest.param({"side_effect": httpx.ConnectTimeout("timed out")}, id="timeout"),
        pytest.param({"return_value": httpx.Response(500)}, id="server-error"),
        pytest.param({"return_value": httpx.Response(403)}, id="refused"),
    ],
)
async def test_an_endpoint_we_cannot_reach_says_so_without_failing(failure):
    """Every one of these is a 200 carrying a negative, not a 502.

    `POST /auth/connect` answers 502 for all of them together, which is right for
    a flow that has already started and useless for deciding whether to start one.
    """
    respx.get(ISS + WELL_KNOWN).mock(**failure)

    response = await _check()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "unreachable"
    assert body["reachable"] is False
    assert body["smartCapable"] is False


@respx.mock
async def test_a_host_that_no_longer_resolves_reads_as_unreachable(monkeypatch):
    async def _nxdomain(host: str) -> list[str]:
        raise targets.UnresolvedTarget(f"{host} does not resolve")

    monkeypatch.setattr(targets, "_resolve", _nxdomain)

    response = await _check("https://closed-last-year.example/fhir")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unreachable"
    assert not respx.calls, "a name that does not resolve is not worth a connection"


# --- safe to hand a URL ------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    "iss",
    [
        pytest.param("file:///etc/passwd", id="not-http"),
        pytest.param("http://127.0.0.1:5432/fhir", id="loopback"),
        pytest.param("http://169.254.169.254/latest/meta-data", id="instance-metadata"),
        pytest.param("https://user:pw@8.8.8.8/fhir", id="carries-credentials"),
    ],
)
async def test_an_issuer_we_refuse_costs_no_outbound_request(iss):
    """The security property, asserted as the absence of a request.

    Refusing only after fetching would still leave this endpoint usable to learn
    what answers on an address reachable only from in here. Nothing is registered
    with respx deliberately: an attempted request would raise rather than be
    quietly served, so the guard cannot pass this by being fast.
    """
    response = await _check(iss)

    assert response.status_code == 400, response.text
    assert not respx.calls, "the guard let a request out before refusing"
    # The refusal says what the rule is; it does not read the URL back out. A
    # message built from the input would make this a way to place text of one's
    # choosing in a response.
    assert iss not in response.json()["detail"]


@respx.mock
async def test_what_an_endpoint_says_is_not_repeated_back_to_the_caller():
    """`detail` is ours, so an endpoint cannot choose what this API tells a user.

    The underlying error carries the URL that was fetched and the parser's
    complaint about the document, and passing either through would make an
    endpoint's output part of our response.
    """
    respx.get(ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(
            200,
            json={
                "authorization_endpoint": "javascript:alert(1)",
                "token_endpoint": "https://example-hospital.org/token",
                "note": "<script>alert('reflected')</script>",
            },
        )
    )

    body = (await _check()).json()

    assert body["status"] == "invalid_smart_configuration"
    assert body["detail"] == (
        "The endpoint publishes a SMART configuration that cannot be used to sign in."
    )
    for value in body.values():
        assert "script" not in str(value)
        assert "javascript:" not in str(value)
        assert WELL_KNOWN not in str(value)


# --- what the answer costs, and what it saves --------------------------------


@respx.mock
async def test_a_check_reports_when_it_read_the_endpoint_not_when_it_answered(
    public_smart_config,
):
    """A second check inside the cache window reports the first fetch's time.

    Claiming "checked just now" off a cached document would reintroduce, in
    miniature, the staleness this endpoint exists to replace.
    """
    route = respx.get(ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=public_smart_config)
    )

    first = (await _check()).json()
    second = (await _check()).json()

    assert route.call_count == 1
    assert second["checkedAt"] == first["checkedAt"]


@respx.mock
async def test_checking_first_leaves_nothing_for_the_connect_to_repeat(
    public_smart_config,
):
    """The pre-flight is not extra work: it warms the cache the flow then uses.

    Both go through the shared discovery instance, so the check a user pays for
    before choosing is the fetch `POST /auth/connect` would have made anyway.
    """
    route = respx.get(ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=public_smart_config)
    )

    await _check()
    await registry.discovery.fetch(ISS)

    assert route.call_count == 1


@respx.mock
async def test_the_throttle_is_wired_to_the_route(
    public_smart_config, low_check_rate_limit
):
    """Asserted rather than read off the decorator, because this limit is the only
    thing bounding how fast this backend can be aimed at something."""
    respx.get(ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=public_smart_config)
    )

    statuses = [(await _check()).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]


# --- the stale flag it is there to replace -----------------------------------


@respx.mock
async def test_a_search_row_dates_its_flag_while_a_check_stamps_the_present(
    public_smart_config,
):
    """Both say SMART-capable; only one of them says when.

    That is the difference a caller has to be able to see, since one answer is a
    certification record and the other is this minute.
    """
    _mock_published_file()
    respx.get(ISS + WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=public_smart_config)
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        rows = (await client.get("/providers/search")).json()
    checked = (await _check()).json()

    dated = [row for row in rows["rows"] if row["smartCapable"]]
    assert dated, "the stand-in file should carry a SMART-capable row"
    assert all(row["smartCapableAsOf"] == FILE_DATE for row in dated)
    assert checked["checkedAt"] > FILE_DATE, (
        "the live answer should be newer than the file the flag came from"
    )


# --- opt-in: the real servers. Run with `pytest -m live`. ---------------------


@pytest.mark.live
async def test_live_check_tells_three_real_endpoints_apart():
    answers = {}
    for label, iss in (
        ("ok", "https://launch.smarthealthit.org/v/r4/fhir"),
        ("no_smart_configuration", "https://hapi.fhir.org/baseR4"),
        ("unreachable", "https://not-a-real-fhir-server.invalid/R4"),
    ):
        answers[label] = (await _check(iss)).json()["status"]

    assert answers == {
        "ok": "ok",
        "no_smart_configuration": "no_smart_configuration",
        "unreachable": "unreachable",
    }
