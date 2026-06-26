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

    # Base URL of the frontend that handles the OAuth redirect. The provider
    # registration's redirect_uri is built from this, so it must match what is
    # registered with each EHR.
    frontend_hostname: str = "http://localhost:3000"

    # OAuth client credentials, registered per environment with the EHR. These
    # are secrets and so live in the environment, never in source. Optional
    # because a deployment only configures the providers it actually uses.
    epic_sandbox_client_id: str | None = None
    epic_sandbox_client_secret: str | None = None
    epic_client_id: str | None = None
    epic_client_secret: str | None = None

    # The FHIR base URL (issuer) of the production Epic deployment, used to
    # allowlist the issuer a caller may start an authorization against. Unset
    # leaves that provider with no allowed issuer, so it rejects every request.
    epic_issuer: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so the environment is read once."""
    return Settings()
