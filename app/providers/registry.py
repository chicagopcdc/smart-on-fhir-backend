"""Which adapter serves a connection, and the discovery cache they all share.

A stored connection names its provider key and its issuer and nothing else, so
anything that has to reach that server again — refreshing a token, revoking one —
has to rebuild the adapter from the registration. That lives here rather than
beside the API's dependencies so the auth layer can reach it without importing
the routers.
"""

from __future__ import annotations

from app.providers import config
from app.providers.discovery import SMARTDiscovery
from app.providers.generic import GenericSMARTProvider


class ProviderNotConfigured(Exception):
    """This deployment holds no usable registration for a provider key.

    Reachable whenever a connection outlives the configuration that created it:
    a provider dropped from ``EHR_CONFIGS``, or credentials removed from the
    environment. The stored token is still there; there is just no longer a
    client identity to present alongside it.
    """


# One discovery cache shared across requests: re-authorizing the same issuer
# reuses its SMART configuration instead of re-fetching it each time.
discovery = SMARTDiscovery()


def provider_for(ehr: dict, iss: str) -> GenericSMARTProvider:
    """Build the discovery-driven provider for one provider/issuer connection.

    The token is bound to the issuer the app intends to call, so aud is the
    issuer, never a hardcoded server.
    """
    return GenericSMARTProvider(
        client_id=ehr["client_id"],
        client_secret=ehr.get("client_secret"),
        redirect_uri=ehr["redirect_uri"],
        aud=iss.rstrip("/"),
        discovery=discovery,
    )


def for_connection(provider: str, iss: str) -> GenericSMARTProvider:
    """The adapter for a connection that already exists, from what its row names.

    No allowlist check, unlike the authorization path: the issuer was checked
    against it when the connection was made, and it is stored rather than
    supplied. What can have changed since is the registration on our side, which
    is the one thing this does verify.
    """
    ehr = config.EHR_CONFIGS.get(provider)
    if ehr is None or not ehr.get("client_id"):
        raise ProviderNotConfigured(provider)
    return provider_for(ehr, iss)
