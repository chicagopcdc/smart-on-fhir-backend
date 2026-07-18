"""`GET /providers` lists the providers this backend can authorize against, so the
frontend can pin them above the Lantern list (they are not in Lantern). The flow test
drives the endpoint; the focused test proves a provider without credentials is omitted,
since `/auth/start` would reject it and offering it would be a dead end.
"""

from __future__ import annotations

import httpx

from app import main
from app.providers import config


async def test_providers_lists_the_configured_epic_sandbox():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        r = await client.get("/providers")

    assert r.status_code == 200
    epic = [p for p in r.json() if p["provider"] == "EPIC_SANDBOX"]
    assert epic, "the credentialed Epic sandbox should be offered"
    assert epic[0]["iss"] == "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
    assert epic[0]["name"] == "Epic Sandbox"


def test_a_provider_without_credentials_is_omitted(monkeypatch):
    monkeypatch.setattr(
        config,
        "EHR_CONFIGS",
        {
            "READY": {"client_id": "abc", "allowed_issuers": ["https://ready.example/fhir"]},
            "NOCREDS": {"client_id": None, "allowed_issuers": ["https://nocreds.example/fhir"]},
            # Same issuer as READY: the frontend keys its options by issuer, so it is
            # listed once (the first provider configured for it wins).
            "ALSO_READY": {"client_id": "xyz", "allowed_issuers": ["https://ready.example/fhir"]},
        },
    )

    listed = config.configured_providers()

    assert {p["provider"] for p in listed} == {"READY"}  # NOCREDS dropped, dupe collapsed
    assert listed[0]["iss"] == "https://ready.example/fhir"
    assert listed[0]["name"] == "Ready"
