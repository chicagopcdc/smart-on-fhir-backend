"""Runtime SMART discovery.

A SMART server publishes its OAuth endpoints and capabilities at
``{fhir_base}/.well-known/smart-configuration``. Fetching this at runtime
removes the need to hardcode authorize/token URLs per provider.

The document lives at the FHIR base URL, not the OAuth base: for Epic that is
``.../interconnect-fhir-oauth/api/FHIR/R4``, not ``.../interconnect-fhir-oauth``.
"""

from __future__ import annotations

import json
import time

import httpx
from pydantic import ValidationError

from providers.models import SMARTConfiguration

_WELL_KNOWN_PATH = "/.well-known/smart-configuration"


class SMARTDiscoveryError(Exception):
    """Base class for discovery failures."""


class DiscoveryNotFoundError(SMARTDiscoveryError):
    """The server returned 404 — it publishes no SMART configuration."""


class DiscoveryUnreachableError(SMARTDiscoveryError):
    """The server could not be reached, or returned a non-404 error status."""


class DiscoveryParseError(SMARTDiscoveryError):
    """The document was fetched but is not a valid SMART configuration."""


class SMARTDiscovery:
    """Fetches and caches SMART configuration documents, keyed by issuer."""

    def __init__(self, cache_ttl: float = 900.0, timeout: float = 10.0) -> None:
        self._cache_ttl = cache_ttl
        self._timeout = timeout
        self._cache: dict[str, tuple[float, SMARTConfiguration]] = {}

    async def fetch(self, iss: str) -> SMARTConfiguration:
        """Return the parsed SMART configuration for ``iss``."""
        # Normalize so a trailing slash neither splits the cache nor doubles up.
        base = iss.rstrip("/")

        cached = self._cache.get(base)
        if cached is not None:
            expires_at, config = cached
            if expires_at > time.monotonic():
                return config

        url = base + _WELL_KNOWN_PATH

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise DiscoveryNotFoundError(
                    f"No SMART configuration at {url} (HTTP 404)"
                ) from exc
            raise DiscoveryUnreachableError(
                f"{url} returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise DiscoveryUnreachableError(f"Could not reach {url}: {exc!r}") from exc

        try:
            config = SMARTConfiguration.model_validate(response.json())
        except (json.JSONDecodeError, ValidationError) as exc:
            raise DiscoveryParseError(
                f"Malformed SMART configuration at {url}: {exc}"
            ) from exc

        if self._cache_ttl > 0:
            self._cache[base] = (time.monotonic() + self._cache_ttl, config)
        return config
