"""Contract tests for the FHIRProvider abstraction."""

from __future__ import annotations

import pytest

from providers.base import FHIRProvider
from providers.models import SMARTConfiguration


def test_base_provider_cannot_be_instantiated():
    with pytest.raises(TypeError):
        FHIRProvider()


def test_concrete_subclass_can_be_instantiated(make_provider):
    assert isinstance(make_provider(), FHIRProvider)


async def test_discover_delegates_to_injected_discovery(make_provider):
    config = SMARTConfiguration.model_validate(
        {
            "authorization_endpoint": "https://ehr.example/auth",
            "token_endpoint": "https://ehr.example/token",
        }
    )

    class _RecordingDiscovery:
        def __init__(self) -> None:
            self.fetched: list[str] = []

        async def fetch(self, iss: str) -> SMARTConfiguration:
            self.fetched.append(iss)
            return config

    fake = _RecordingDiscovery()
    result = await make_provider(fake).discover("https://ehr.example/fhir")

    assert result is config
    assert fake.fetched == ["https://ehr.example/fhir"]
