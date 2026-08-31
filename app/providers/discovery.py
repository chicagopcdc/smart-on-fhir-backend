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
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError

from app.providers.models import SMARTConfiguration

_WELL_KNOWN_PATH = "/.well-known/smart-configuration"


class SMARTDiscoveryError(Exception):
    """Base class for discovery failures.

    ``status_code`` is what the server answered with, where it answered at all,
    carried on the exception rather than only written into its message. The
    message names the URL fetched and quotes the parser's complaint, neither of
    which this application repeats back to a caller or writes to a log, so a
    status left only in there would be readable by parsing prose and no other
    way. It is also what separates the two failures ``DiscoveryUnreachableError``
    covers: a server refusing an unauthenticated request is a settled answer, a
    connection that was never made is a bad moment worth retrying.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DiscoveryNotFoundError(SMARTDiscoveryError):
    """The server returned 404 — it publishes no SMART configuration."""


class DiscoveryUnreachableError(SMARTDiscoveryError):
    """The server could not be reached, or returned a non-404 error status."""


class DiscoveryParseError(SMARTDiscoveryError):
    """The document was fetched but is not a valid SMART configuration."""


def failure_status(exc: SMARTDiscoveryError) -> str:
    """Which failure this is, in the vocabulary the API answers checks in.

    Lives beside the exceptions rather than at either caller, because there are
    two — the endpoint check answers it to a caller, and an authorization writes
    it to the log — and a fifth failure added here has to reach both. Restated at
    one of them, a new case would quietly read as ``unreachable`` on that side
    only, while the other reported it correctly.

    The strings are ``EndpointCheckStatus`` in ``app/api/schemas.py``; naming that
    type here would point this module at the API layer, which it otherwise knows
    nothing about.
    """
    if isinstance(exc, DiscoveryNotFoundError):
        return "no_smart_configuration"
    if isinstance(exc, DiscoveryParseError):
        return "invalid_smart_configuration"
    return "unreachable"


@dataclass(frozen=True)
class DiscoveryResult:
    """A configuration and when it was read off the network, so a caller reporting
    how current its answer is stays honest across a cache hit."""

    configuration: SMARTConfiguration
    fetched_at: datetime


class SMARTDiscovery:
    """Fetches and caches SMART configuration documents, keyed by issuer.

    The cache holds successes only, for ``cache_ttl`` seconds and at most
    ``max_entries`` of them. Failures are deliberately not cached: a server that
    blips would otherwise stay unusable for the rest of the window, which is a far
    worse trade than re-asking.
    """

    def __init__(
        self,
        cache_ttl: float = 900.0,
        timeout: float = 10.0,
        max_entries: int = 512,
    ) -> None:
        self._cache_ttl = cache_ttl
        self._timeout = timeout
        self._max_entries = max_entries
        self._cache: dict[str, tuple[float, DiscoveryResult]] = {}

    def clear(self) -> None:
        """Drop every cached configuration, forcing the next fetch to re-discover."""
        self._cache.clear()

    async def fetch(self, iss: str) -> SMARTConfiguration:
        """Return the parsed SMART configuration for ``iss``."""
        return (await self.fetch_result(iss)).configuration

    async def fetch_result(self, iss: str) -> DiscoveryResult:
        """Return the configuration for ``iss`` alongside when it was fetched."""
        # Normalize so a trailing slash neither splits the cache nor doubles up.
        base = iss.rstrip("/")

        cached = self._cache.get(base)
        if cached is not None:
            expires_at, result = cached
            if expires_at > time.monotonic():
                return result

        url = base + _WELL_KNOWN_PATH

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise DiscoveryNotFoundError(
                    f"No SMART configuration at {url} (HTTP 404)", status_code=404
                ) from exc
            raise DiscoveryUnreachableError(
                f"{url} returned HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise DiscoveryUnreachableError(f"Could not reach {url}: {exc!r}") from exc

        try:
            config = SMARTConfiguration.model_validate(response.json())
        except (json.JSONDecodeError, ValidationError) as exc:
            raise DiscoveryParseError(
                f"Malformed SMART configuration at {url}: {exc}"
            ) from exc

        result = DiscoveryResult(
            configuration=config, fetched_at=datetime.now(timezone.utc)
        )
        if self._cache_ttl > 0:
            # Removed first so a re-fetch moves to the back. Assigning to a key that
            # is already present keeps its original position, which would make the
            # entry that was just refreshed the next one evicted.
            self._cache.pop(base, None)
            self._cache[base] = (time.monotonic() + self._cache_ttl, result)
            self._evict()
        return result

    def _evict(self) -> None:
        """Keep the cache bounded.

        Once a caller can name the issuer, an unbounded cache is a way to spend this
        process's memory: these documents run to tens of kilobytes each.

        Expired entries go first, then the oldest insertions, since dicts preserve
        insertion order. Evicting a live entry costs one re-fetch and nothing else,
        which is why a plain bound is enough and tracking access order would not
        earn its keep.
        """
        now = time.monotonic()
        expired = [
            key for key, (expires_at, _) in self._cache.items() if expires_at <= now
        ]
        for key in expired:
            del self._cache[key]

        while len(self._cache) > self._max_entries:
            del self._cache[next(iter(self._cache))]
