"""Application configuration loaded from the environment.

pydantic-settings reads these values from the process environment and, in local
development, from a ``.env`` file (the real ``.env`` is gitignored; ``.env.example``
documents every key). This is the working replacement for the previously declared
but never-called ``python-dotenv`` / ``load_dotenv()`` path.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings, validated on first access."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Async SQLAlchemy connection string (asyncpg driver) for the Postgres store.
    # Required: a forgotten DATABASE_URL should fail at startup, not silently
    # connect to a default that only exists on a dev machine.
    database_url: str

    # One or more Fernet keys, comma-separated, used to encrypt tokens at rest.
    # The first key encrypts; the rest are still accepted for decryption, which
    # is what allows rotating the key without invalidating stored tokens.
    # Intentionally has no default: a missing key should fail loudly rather
    # than store tokens in plaintext.
    token_encryption_key: str

    # Lifetime of an OAuth state row before the TTL sweep removes it.
    oauth_state_ttl_seconds: int = 600

    # Lifetime of an app session (the bearer token the frontend uses to read
    # resources after authorizing) before it is swept and the caller must
    # re-authorize. Default one hour.
    app_session_ttl_seconds: int = 3600

    # How close to its expiry a stored access token may be before a read
    # refreshes it first. Zero would leave every read racing the clock it just
    # checked: the token has to survive the fan-out of FHIR calls that follows,
    # not merely the instant it was inspected.
    token_refresh_leeway_seconds: int = 60

    # Base URL of the frontend that handles the OAuth redirect. The provider
    # registration's redirect_uri is built from this, so it must match what is
    # registered with each EHR.
    frontend_hostname: str = "http://localhost:3000"

    # Browser origins allowed to call the API (CORS). Comma-separated; when unset
    # it defaults to the frontend host. A wildcard is deliberately unsupported —
    # the API is only meant to be called from the known frontend.
    cors_allowed_origins: str | None = None

    # Per-client throttles for the auth and resource endpoints, in slowapi's
    # "<count>/<window>" syntax. rate_limit_enabled turns throttling off wholesale
    # for single-user local runs and the test suite.
    rate_limit_enabled: bool = True
    auth_rate_limit: str = "10/minute"
    fhir_rate_limit: str = "30/minute"

    # OAuth client credentials, registered per environment with the EHR. These
    # are secrets and so live in the environment, never in source. Optional
    # because a deployment only configures the providers it actually uses.
    epic_sandbox_client_id: str | None = None
    epic_sandbox_client_secret: str | None = None
    epic_client_id: str | None = None
    epic_client_secret: str | None = None

    # Cerner / Oracle Health sandbox. Registered as a public client, so it has a
    # client_id but no secret: PKCE stands in for client authentication. Leave
    # the id unset until an app is registered in Oracle Health's code Console;
    # the provider rejects every request until then.
    cerner_client_id: str | None = None

    # Public SMART App Launcher (launch.smarthealthit.org). It is a shared test
    # server that does not validate the client_id, so a default lets the flow run
    # out of the box; override only to mirror a specific registration.
    smart_launcher_client_id: str | None = None

    # The FHIR base URL (issuer) of the production Epic deployment, used to
    # allowlist the issuer a caller may start an authorization against. Unset
    # leaves that provider with no allowed issuer, so it rejects every request.
    epic_issuer: str | None = None

    @property
    def resolved_cors_origins(self) -> list[str]:
        """The concrete list of allowed browser origins, frontend host by default.

        Trailing slashes are stripped: a browser ``Origin`` header never carries
        one, so an origin like ``https://app.example.org/`` would otherwise never
        match and silently break CORS for the whole frontend.
        """
        raw = self.cors_allowed_origins or self.frontend_hostname
        return [
            stripped.rstrip("/")
            for origin in raw.split(",")
            if (stripped := origin.strip())
        ]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so the environment is read once."""
    return Settings()
