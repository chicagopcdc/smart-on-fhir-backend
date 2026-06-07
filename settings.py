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
    database_url: str = (
        "postgresql+asyncpg://postgres:devpass@localhost:5432/smartfhir"
    )

    # Fernet key used to encrypt tokens at rest. Intentionally has no default:
    # a missing key should fail loudly rather than store tokens in plaintext.
    token_encryption_key: str

    # Lifetime of an OAuth state row before the TTL sweep removes it.
    oauth_state_ttl_seconds: int = 600


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so the environment is read once."""
    return Settings()
