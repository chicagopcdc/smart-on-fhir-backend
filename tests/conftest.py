"""Shared fixtures and a minimal concrete provider for the test suite."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

# Settings validate on first use. Force a throwaway encryption key and an
# offline SQLite DSN before importing any module that reads them — assigned
# unconditionally so a key or DSN exported in the developer's shell can never
# leak into the suite.
os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["OAUTH_STATE_TTL_SECONDS"] = "600"
os.environ["EPIC_SANDBOX_CLIENT_ID"] = "test-sandbox-client-id"
os.environ["EPIC_SANDBOX_CLIENT_SECRET"] = "test-sandbox-client-secret"
# A public-client sandbox registered without a secret; PKCE stands in for a
# client secret, so no CERNER_CLIENT_SECRET is set.
os.environ["CERNER_CLIENT_ID"] = "test-cerner-client-id"
os.environ["FRONTEND_HOSTNAME"] = "http://localhost:3000"
# Throttling off by default so request-heavy flow tests are never rate limited;
# the one rate-limit test re-enables it for its own duration.
os.environ["RATE_LIMIT_ENABLED"] = "false"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.auth.models import Base  # noqa: E402
from app.providers.base import FHIRProvider  # noqa: E402
from app.providers.models import (  # noqa: E402
    AuthorizationRequest,
    SMARTConfiguration,
    TokenSet,
)
from tests.app_harness import load_fixture  # noqa: E402


class StubProvider(FHIRProvider):
    """Concrete provider that implements just enough to drive discover()."""

    def build_auth_url(self, config, state, scopes) -> AuthorizationRequest:
        return AuthorizationRequest(url=f"{config.authorization_endpoint}?state={state}")

    async def exchange_token(self, config, code, code_verifier=None) -> TokenSet:
        raise NotImplementedError

    async def refresh_token(self, config, refresh_token) -> TokenSet:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _reset_discovery_cache():
    """Keep the app's shared discovery cache from leaking configs between tests."""
    from app.api import deps

    deps.discovery.clear()
    yield
    deps.discovery.clear()


@pytest.fixture
def make_provider():
    def _make(discovery=None) -> StubProvider:
        return StubProvider(discovery=discovery)

    return _make


@pytest.fixture
def epic_smart_config() -> dict:
    return load_fixture("epic_smart_config.json")


@pytest.fixture
def public_smart_config() -> dict:
    return load_fixture("smarthealthit_smart_config.json")


@pytest_asyncio.fixture
async def db_session():
    """An isolated in-memory SQLite session with the schema created.

    StaticPool keeps a single connection so the in-memory database persists for
    the life of the test; the schema is built from the ORM metadata.
    """
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
def epic_token_response() -> dict:
    # The __epic.dstu2.patient key is vendor-specific; tests assert it is dropped.
    return {
        "access_token": "PLACEHOLDER.access.jwt",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "patient/Patient.read launch/patient offline_access openid profile",
        "refresh_token": "PLACEHOLDER.refresh.jwt",
        "__epic.dstu2.patient": "TnOZ.elPXC6zcBNFMcFA7A5KZbYxo2.4T-LylRk4GoW4B",
        "id_token": "PLACEHOLDER.id.jwt",
        "patient": "erXuFYUfucBZaryVksYEcMg3",
    }
