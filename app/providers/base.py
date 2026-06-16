"""The provider adapter contract.

Providers talk to SMART on FHIR servers. The intended design is a single
discovery-driven provider that adapts to what each server advertises, rather
than a subclass per vendor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.providers.discovery import SMARTDiscovery
from app.providers.models import AuthorizationRequest, SMARTConfiguration, TokenSet


class FHIRProvider(ABC):
    """Abstract adapter for a SMART on FHIR server."""

    def __init__(self, discovery: SMARTDiscovery | None = None) -> None:
        # Injectable so tests can supply a fake; a shared instance shares its cache.
        self._discovery = discovery or SMARTDiscovery()

    async def discover(self, iss: str) -> SMARTConfiguration:
        """Fetch and parse the server's SMART configuration."""
        return await self._discovery.fetch(iss)

    @abstractmethod
    def build_auth_url(
        self,
        config: SMARTConfiguration,
        state: str,
        scopes: list[str],
    ) -> AuthorizationRequest:
        """Build the authorization redirect, plus any PKCE verifier to retain.

        Returns an :class:`AuthorizationRequest` rather than a bare URL so the
        caller can persist the PKCE ``code_verifier`` alongside the OAuth state
        and replay it at the token exchange.
        """
        ...

    @abstractmethod
    async def exchange_token(
        self,
        config: SMARTConfiguration,
        code: str,
        code_verifier: str | None = None,
    ) -> TokenSet:
        """Exchange an authorization code for tokens."""
        ...

    @abstractmethod
    async def refresh_token(
        self, config: SMARTConfiguration, refresh_token: str
    ) -> TokenSet:
        """Obtain a fresh token set from a refresh token."""
        ...
