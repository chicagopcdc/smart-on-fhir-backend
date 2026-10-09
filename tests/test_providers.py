"""`GET /providers` lists the providers this backend can authorize against, so the
frontend can pin them above the Lantern list (they are not in Lantern). The flow test
drives the endpoint; the focused test proves a provider without credentials is omitted,
since `/auth/connect` would reject it and offering it would be a dead end.
"""

from __future__ import annotations

from app.providers import config
from tests.app_harness import client as _client


async def test_providers_lists_the_configured_epic_sandbox():
    async with _client() as client:
        r = await client.get("/providers")

    assert r.status_code == 200
    body = r.json()

    epic = [p for p in body if p["provider"] == "EPIC_SANDBOX"]
    assert epic, "the credentialed Epic sandbox should be offered"
    assert epic[0]["iss"] == "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
    assert epic[0]["name"] == "Epic Sandbox"

    # The dropdown is built straight from these keys, so the response model has to
    # serialize to exactly them: the camelCase alias generator must not rename a
    # field out from under a caller that has read it since before there was a model.
    assert all(set(entry) == {"provider", "iss", "name"} for entry in body)


def test_a_provider_without_credentials_is_omitted():
    listed = config.configured_providers(
        {
            "READY": {"client_id": "abc", "allowed_issuers": ["https://ready.example/fhir"]},
            "NOCREDS": {"client_id": None, "allowed_issuers": ["https://nocreds.example/fhir"]},
            # Same issuer as READY: the frontend keys its options by issuer, so it is
            # listed once (the first provider configured for it wins).
            "ALSO_READY": {"client_id": "xyz", "allowed_issuers": ["https://ready.example/fhir"]},
        }
    )

    assert {p["provider"] for p in listed} == {"READY"}  # NOCREDS dropped, dupe collapsed
    assert listed[0]["iss"] == "https://ready.example/fhir"
    assert listed[0]["name"] == "Ready"


def test_configured_providers_empty_issuers_and_trailing_slashes():
    """Verify that empty/falsey issuers are ignored, trailing slashes are stripped,
    and provider names are properly capitalized from snake_case / uppercase keys.
    """
    listed = config.configured_providers(
        {
            "MY_CUSTOM_EHR": {
                "client_id": "client-123",
                "allowed_issuers": [
                    "",
                    None,
                    "https://fhir.example.com/r4/",
                    "https://fhir.example.com/r4",  # duplicate after rstrip('/')
                ],
            },
            "ANOTHER_PROVIDER": {
                "client_id": "",  # empty string client_id should be skipped
                "allowed_issuers": ["https://other.example.com/fhir"],
            },
        }
    )

    assert len(listed) == 1
    assert listed[0] == {
        "provider": "MY_CUSTOM_EHR",
        "iss": "https://fhir.example.com/r4",
        "name": "My Custom Ehr",
    }


def test_configured_providers_deduplicates_issuers_across_providers():
    """When multiple valid providers configure the same canonical issuer,
    the issuer appears only once, preserving the first provider that declared it.
    """
    test_configs = {
        "PRIMARY_EHR": {
            "client_id": "pri-client",
            "allowed_issuers": ["https://shared.example.com/fhir/"],
        },
        "SECONDARY_EHR": {
            "client_id": "sec-client",
            "allowed_issuers": ["https://shared.example.com/fhir"],
        },
    }
    listed = config.configured_providers(test_configs)

    assert len(listed) == 1
    assert listed[0]["provider"] == "PRIMARY_EHR"
    assert listed[0]["iss"] == "https://shared.example.com/fhir"
    assert listed[0]["name"] == "Primary Ehr"


def test_configured_providers_isolated_from_module_ehr_configs():
    """Passing an explicit empty or custom configs dictionary does not fall back
    to or modify module-level EHR_CONFIGS.
    """
    empty_listed = config.configured_providers({})
    assert empty_listed == []

    custom_listed = config.configured_providers(
        {
            "TEST_PROVIDER": {
                "client_id": "cid",
                "allowed_issuers": ["https://isolated.test/fhir"],
            }
        }
    )
    assert len(custom_listed) == 1
    assert custom_listed[0]["provider"] == "TEST_PROVIDER"
    assert custom_listed[0]["iss"] == "https://isolated.test/fhir"
    assert custom_listed[0]["name"] == "Test Provider"

