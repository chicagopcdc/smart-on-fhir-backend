"""Shared helpers for driving the live ASGI app against a throwaway database.

The full-flow tests all bind ``main.app`` to a per-test SQLite database and talk
to it through an in-process ASGI transport. Keeping that scaffolding here means
the flow tests share one definition instead of each carrying their own copy.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.auth.models import Base
from app.core.db import get_session

_FIXTURES = Path(__file__).parent / "fixtures"


# The servers the suite drives, described by what the authorization flow needs of
# them. Held here so that re-registering a sandbox, or a vendor moving its token
# endpoint, is one edit rather than one per test module.
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
}
CERNER_SANDBOX = {
    "provider": "CERNER_SANDBOX",
    "iss": "https://fhir-ehr-code.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d",
    "authorize_url": "https://authorization.cerner.com/tenants/ec2458f2-1e24-41c8-b71b-0e701af7583d/protocols/oauth2/profiles/smart-v1/personas/provider/authorize",
    "token_url": "https://authorization.cerner.com/tenants/ec2458f2-1e24-41c8-b71b-0e701af7583d/hosts/fhir-ehr-code.cerner.com/protocols/oauth2/profiles/smart-v1/token",
    "smart_config": "cerner_smart_config.json",
}


def load_fixture(name: str) -> dict:
    """Load a JSON fixture (e.g. a saved SMART discovery document) by file name."""
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


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
    try:
        yield factory
    finally:
        main.app.dependency_overrides.clear()
        await engine.dispose()


def client() -> httpx.AsyncClient:
    """An HTTP client wired to the app over an in-process ASGI transport."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )


async def authorize(client: httpx.AsyncClient, server: dict, token_response: dict) -> str:
    """Run the real authorization flow against a mocked server, returning the session id.

    Tests that read resources need a persisted token to read them with, and the
    only way to get one is the flow itself. Registering discovery and the token
    endpoint here (rather than in each test) keeps those routes ahead of any
    catch-all a caller adds afterwards, since respx matches in registration order.

    ``server`` is one of the descriptors above. The caller supplies the
    ``respx.mock`` context.
    """
    respx.get(server["iss"] + "/.well-known/smart-configuration").mock(
        return_value=httpx.Response(200, json=load_fixture(server["smart_config"]))
    )
    respx.post(server["token_url"]).mock(
        return_value=httpx.Response(200, json=token_response)
    )

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
    return callback.json()["session_id"]


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
