"""Model boundary tests: turning external discovery/token JSON into typed data."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.providers.models import SMARTConfiguration, TokenSet


def test_real_epic_doc_parses_with_recommended_endpoints_absent(epic_smart_config):
    cfg = SMARTConfiguration.model_validate(epic_smart_config)
    # Epic omits these; they must parse to None rather than fail.
    assert cfg.introspection_endpoint is None
    assert cfg.revocation_endpoint is None
    assert cfg.management_endpoint is None


def test_real_public_doc_parses_with_recommended_endpoints_present(public_smart_config):
    cfg = SMARTConfiguration.model_validate(public_smart_config)
    assert cfg.introspection_endpoint is not None
    assert cfg.jwks_uri is not None


def test_minimal_doc_defaults_arrays_and_reports_no_pkce():
    cfg = SMARTConfiguration.model_validate(
        {
            "authorization_endpoint": "https://ehr.example/auth",
            "token_endpoint": "https://ehr.example/token",
        }
    )
    assert cfg.scopes_supported == []
    assert cfg.code_challenge_methods_supported == []
    assert cfg.supports_pkce is False
    assert cfg.issuer is None


@pytest.mark.parametrize("missing", ["authorization_endpoint", "token_endpoint"])
def test_missing_required_endpoint_is_rejected(missing):
    doc = {
        "authorization_endpoint": "https://ehr.example/auth",
        "token_endpoint": "https://ehr.example/token",
    }
    doc.pop(missing)
    with pytest.raises(ValidationError):
        SMARTConfiguration.model_validate(doc)


def test_relative_endpoint_url_is_rejected():
    with pytest.raises(ValidationError):
        SMARTConfiguration.model_validate(
            {
                "authorization_endpoint": "/oauth2/authorize",
                "token_endpoint": "https://ehr.example/token",
            }
        )


def test_unknown_top_level_keys_are_ignored():
    cfg = SMARTConfiguration.model_validate(
        {
            "authorization_endpoint": "https://ehr.example/auth",
            "token_endpoint": "https://ehr.example/token",
            "vendor_only_field": "ignore me",
        }
    )
    assert cfg.model_extra is None
    assert "vendor_only_field" not in cfg.model_dump()


def test_token_set_parses_real_shape_and_drops_vendor_keys(epic_token_response):
    ts = TokenSet.model_validate(epic_token_response)
    assert ts.access_token
    assert ts.token_type == "Bearer"
    assert ts.refresh_token
    assert ts.patient == "erXuFYUfucBZaryVksYEcMg3"
    assert ts.model_extra is None
    assert "__epic.dstu2.patient" not in ts.model_dump()


def test_token_set_applies_minimal_defaults():
    ts = TokenSet.model_validate({"access_token": "abc"})
    assert ts.token_type == "Bearer"
    assert ts.refresh_token is None
    assert ts.expires_in is None
