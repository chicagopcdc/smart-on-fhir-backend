"""Shared fixtures and a minimal concrete provider for the test suite."""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

# Settings validate on first use. Force a throwaway encryption key and an
# offline SQLite DSN before importing any module that reads them — assigned
# unconditionally so a key or DSN exported in the developer's shell can never
# leak into the suite.
os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["OAUTH_STATE_TTL_SECONDS"] = "600"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.auth.models import Base  # noqa: E402
from app.providers.base import FHIRProvider  # noqa: E402
from app.providers.models import SMARTConfiguration, TokenSet  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class StubProvider(FHIRProvider):
    """Concrete provider that implements just enough to drive discover()."""

    def build_auth_url(self, config, state, scopes) -> str:
        return f"{config.authorization_endpoint}?state={state}"

    async def exchange_token(self, config, code) -> TokenSet:
        raise NotImplementedError

    async def refresh_token(self, config, refresh_token) -> TokenSet:
        raise NotImplementedError


@pytest.fixture
def make_provider():
    def _make(discovery=None) -> StubProvider:
        return StubProvider(discovery=discovery)

    return _make


@pytest.fixture
def epic_smart_config() -> dict:
    return _load_json("epic_smart_config.json")


@pytest.fixture
def public_smart_config() -> dict:
    return _load_json("smarthealthit_smart_config.json")


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
