"""Shared helpers for driving the live ASGI app against a throwaway database.

The full-flow tests all bind ``main.app`` to a per-test SQLite database and talk
to it through an in-process ASGI transport. Keeping that scaffolding here means
the flow tests share one definition instead of each carrying their own copy.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.auth.models import Base, ProviderToken, utcnow
from app.core import db
from app.core.db import get_session
from app.providers.config import RESOURCE_FETCH_CONFIG, fhir_type_for

_FIXTURES = Path(__file__).parent / "fixtures"

# Which fetch config row a FHIR request came from, keyed the way the request
# identifies itself: the resource type it addresses plus the `category` that
# separates the Observation searches. Inverted from the config rather than
# rebuilt from a naming convention, so a row the config spells differently still
# resolves.
FETCH_KEYS = {
    (fhir_type_for(entry), (entry.get("extra_params") or {}).get("category")): name
    for name, entry in RESOURCE_FETCH_CONFIG.items()
}
FHIR_TYPES = {fhir_type for fhir_type, _ in FETCH_KEYS}


# The servers the suite drives, described by what the flow needs of them: where
# to authorize, where to exchange, what its discovery document says, and — where
# one was captured — the record it really answered a read with. Held here so that
# re-registering a sandbox, or a vendor moving its token endpoint, is one edit
# rather than one per test module.
EPIC_SANDBOX = {
    "provider": "EPIC_SANDBOX",
    "iss": "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
    "authorize_url": "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize",
    "token_url": "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token",
    "smart_config": "epic_smart_config.json",
}
SMART_LAUNCHER = {
    "provider": "SMART_LAUNCHER",
    "iss": "https://launch.smarthealthit.org/v/r4/fhir",
    "authorize_url": "https://launch.smarthealthit.org/v/r4/auth/authorize",
    "token_url": "https://launch.smarthealthit.org/v/r4/auth/token",
    "smart_config": "smarthealthit_smart_config.json",
    "record": "launcher_patient_record.json",
}
CERNER_SANDBOX = {
    "provider": "CERNER_SANDBOX",
    # The patient persona. Cerner serves one tenant at a different host per persona
    # and the host is what decides who may sign in, so the issuer selects between a
    # patient reaching their own record and a clinician reaching their patients'.
    "iss": "https://fhir-myrecord.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d",
    "authorize_url": "https://authorization.cerner.com/tenants/ec2458f2-1e24-41c8-b71b-0e701af7583d/protocols/oauth2/profiles/smart-v1/personas/patient/authorize",
    "token_url": "https://authorization.cerner.com/tenants/ec2458f2-1e24-41c8-b71b-0e701af7583d/hosts/fhir-myrecord.cerner.com/protocols/oauth2/profiles/smart-v1/token",
    "smart_config": "cerner_smart_config.json",
    # Captured from Cerner's open endpoint, which is the unauthenticated view of
    # the same tenant as the configured issuer, so the data is Oracle Health's
    # while the URL is the one the allowlist authorizes.
    "record": "cerner_patient_record.json",
}


def load_fixture(name: str) -> dict:
    """Load a JSON fixture (e.g. a saved SMART discovery document) by file name."""
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def save_fixture(name: str, payload) -> None:
    """Write a fixture back, for a live run re-capturing what a server now says.

    The inverse of ``load_fixture`` and deliberately next to it, so the one place
    that knows where captures live is the one place that writes them. Two spaces
    of indent and a trailing newline to match what is already on disk, which keeps
    a refresh diff to the lines that actually changed.
    """
    (_FIXTURES / name).write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def token_response(patient_id: str, **overrides) -> dict:
    """What a token endpoint returns for a patient, for a mocked exchange.

    The access token names the patient so a test asserting which credential
    reached a server can tell one connection's from another's.
    """
    return {
        "access_token": f"access-for-{patient_id}",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "launch/patient patient/*.read",
        "patient": patient_id,
        **overrides,
    }


def fetch_key(url) -> str | None:
    """The fetch config row a FHIR request came from."""
    parsed = urlparse(str(url))
    resource = next(
        (segment for segment in reversed(parsed.path.split("/")) if segment in FHIR_TYPES),
        None,
    )
    category = parse_qs(parsed.query).get("category", [None])[0]
    return FETCH_KEYS.get((resource, category))


def serve_record(record: dict):
    """Answer each FHIR search with what that server really returned.

    ``record`` is a captured patient record fixture: a patient id and the
    responses that server gave, keyed by fetch config row.
    """

    def responder(request):
        response = record["responses"].get(fetch_key(request.url))
        if response is None:
            return httpx.Response(
                404, json={"resourceType": "OperationOutcome", "issue": []}
            )
        return httpx.Response(response["statusCode"], json=response["body"])

    return responder


@asynccontextmanager
async def app_db(url: str):
    """Bind the app to a throwaway database for the duration of a test."""
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = override_get_session
    # Not every session belongs to a request. A token refresh opens its own, so
    # that it commits a rotated refresh token whatever becomes of the request
    # that triggered it, and it reaches for the module-level factory rather than
    # the dependency. Point that at this database too, or those writes land in
    # the process-wide one.
    process_factory = db.SessionFactory
    db.SessionFactory = factory
    try:
        yield factory
    finally:
        db.SessionFactory = process_factory
        main.app.dependency_overrides.clear()
        await engine.dispose()


def client() -> httpx.AsyncClient:
    """An HTTP client wired to the app over an in-process ASGI transport."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )


def mock_server(server: dict, token_response: dict | Callable) -> None:
    """Register discovery and the token endpoint for one server.

    Called before any catch-all a test adds, since respx matches in registration
    order and a broad FHIR route would otherwise swallow the discovery request.

    ``token_response`` is the body an exchange answers with, or a responder
    called with each request, for tests where one endpoint has to answer a
    later call — a refresh — differently from the exchange before it.
    """
    respx.get(server["iss"] + "/.well-known/smart-configuration").mock(
        return_value=httpx.Response(200, json=load_fixture(server["smart_config"]))
    )
    route = respx.post(server["token_url"])
    if callable(token_response):
        route.mock(side_effect=token_response)
    else:
        route.mock(return_value=httpx.Response(200, json=token_response))


async def authorize(client: httpx.AsyncClient, server: dict, token_response: dict) -> str:
    """Run the real authorization flow against a mocked server, returning the session id.

    Tests that read resources need a persisted token to read them with, and the
    only way to get one is the flow itself.

    ``server`` is one of the descriptors above. The caller supplies the
    ``respx.mock`` context.
    """
    mock_server(server, token_response)

    start = await client.get(
        "/auth/start",
        params={"provider": server["provider"], "iss": server["iss"]},
        follow_redirects=False,
    )
    assert start.status_code == 307, start.text
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    callback = await client.post(
        "/auth/callback", json={"code": "test-auth-code", "state": state}
    )
    assert callback.status_code == 200, callback.text
    return callback.json()["sessionId"]


async def connect(
    client: httpx.AsyncClient,
    server: dict,
    token_response: dict | Callable,
    *,
    link_session: str | None = None,
) -> dict:
    """Authorize through /auth/connect, returning the callback's response body.

    The same flow as ``authorize`` through the endpoints the API documents.
    Passing ``link_session`` presents that session on the connect call, which is
    how a second provider joins an existing patient record.
    """
    mock_server(server, token_response)

    headers = {"Authorization": f"Bearer {link_session}"} if link_session else {}
    started = await client.post(
        "/auth/connect",
        json={"provider": server["provider"], "iss": server["iss"]},
        headers=headers,
    )
    assert started.status_code == 200, started.text
    state = started.json()["state"]

    callback = await client.post(
        "/auth/callback", json={"code": "test-auth-code", "state": state}
    )
    assert callback.status_code == 200, callback.text
    return callback.json()


async def connect_and_serve(
    client: httpx.AsyncClient,
    server: dict,
    *,
    on_refresh=None,
    link_session: str | None = None,
) -> tuple[dict, respx.Route]:
    """Authorize one server, then start answering its FHIR calls from its record.

    Returns the callback body and the FHIR route, which is where a test looks to
    see which credential the reads actually carried.
    """
    record = load_fixture(server["record"])
    body = await connect(
        client,
        server,
        token_endpoint(record["patientId"], on_refresh=on_refresh),
        link_session=link_session,
    )
    # Registered after connect() so the discovery route keeps priority.
    route = respx.get(url__startswith=server["iss"]).mock(side_effect=serve_record(record))
    return body, route


# --- the token endpoint over a connection's whole life -------------------------


def token_endpoint(patient_id: str, *, on_refresh=None) -> Callable:
    """One server's token endpoint, serving both an exchange and every refresh.

    It rotates: each answer carries a new refresh token, which is what a server
    that retires the one it was handed does. That makes storing the replacement
    the difference between one working refresh and all of them, and the numbering
    is what lets a test say which token was spent when.

    ``on_refresh`` takes over the answer to a refresh, and is handed the issuer
    so a test wanting a *successful* but wrong one can still get a live body.
    """
    issued = itertools.count(1)

    def granted(**overrides) -> dict:
        nth = next(issued)
        return token_response(
            patient_id,
            access_token=f"access-{nth}",
            refresh_token=f"refresh-{nth}",
            **overrides,
        )

    def responder(request: httpx.Request) -> httpx.Response:
        if form(request)["grant_type"] == "refresh_token" and on_refresh is not None:
            return on_refresh(granted)
        return httpx.Response(200, json=granted())

    return responder


def refuse(_granted) -> httpx.Response:
    """The token endpoint saying this authorization is finished, per RFC 6749."""
    return httpx.Response(400, json={"error": "invalid_grant"})


def refreshes(server: dict | None = None) -> list[dict[str, str]]:
    """Every refresh asked of a token endpoint so far, as the form body sent.

    Read off respx's own call log rather than tracked by the responder, so it
    counts what actually left the application rather than what a fake believes
    it was asked for.
    """
    return [
        form(call.request)
        for call in respx.calls
        if call.request.method == "POST"
        and b"grant_type=refresh_token" in call.request.content
        and (server is None or str(call.request.url) == server["token_url"])
    ]


# --- reading and moving what is stored -----------------------------------------


def bearer(session_id: str) -> dict:
    """The header a caller presents to read the record a session holds."""
    return {"Authorization": f"Bearer {session_id}"}


def form(request: httpx.Request) -> dict[str, str]:
    """A posted form body, flattened, for saying what was actually sent."""
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


async def age_tokens(factory, *, to=None) -> None:
    """Move every stored expiry, the way an hour of wall clock would have."""
    async with factory() as session:
        await session.execute(
            update(ProviderToken).values(expires_at=to or utcnow() - timedelta(seconds=1))
        )
        await session.commit()


async def stored_connections(factory) -> list[ProviderToken]:
    """Every connection in the database, oldest first."""
    async with factory() as session:
        result = await session.execute(select(ProviderToken).order_by(ProviderToken.id))
        return list(result.scalars().all())


async def read_resources(
    server: dict,
    db_url: str,
    *,
    token_response: dict,
    responder,
    params: dict | None = None,
):
    """Authorize, then read the record back through a mocked FHIR API.

    Returns ``(response, fhir_route)`` — the tier tests assert on which requests
    the route saw, the flow tests on what came back. ``responder`` is called with
    each outbound FHIR request and returns the reply that server would give.
    """
    async with app_db(db_url):
        async with client() as http:
            session_id = await authorize(http, server, token_response)
            # Registered after authorize() so the discovery route keeps priority.
            fhir_route = respx.get(url__startswith=server["iss"]).mock(
                side_effect=responder
            )
            response = await http.get(
                "/fhir_resources",
                params=params or {},
                headers={"Authorization": f"Bearer {session_id}"},
            )
    return response, fhir_route
