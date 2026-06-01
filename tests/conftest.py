"""Shared fixtures and a minimal concrete provider for the discovery tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from providers.base import FHIRProvider
from providers.models import SMARTConfiguration, TokenSet

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
