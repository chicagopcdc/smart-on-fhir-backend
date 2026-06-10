"""Database models: persisted OAuth state and encrypted provider tokens.

Typed SQLAlchemy 2.0 ORM. Timestamps are UTC and stored naive (no tzinfo) so the
models behave identically on Postgres (production) and SQLite (the offline test
engine), which disagree on how they round-trip timezone-aware values.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core import crypto


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (value is UTC, tzinfo stripped)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base shared by every model; Alembic reads its metadata."""


class EncryptedString(TypeDecorator):
    """A Text column encrypted on write and decrypted on read.

    Encryption lives in the column type rather than in callers, so every read
    and write path is covered and application code never handles ciphertext.
    The value stored in the database is Fernet ciphertext.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        return crypto.encrypt(value) if value is not None else None

    def process_result_value(self, value: str | None, dialect) -> str | None:
        return crypto.decrypt(value) if value is not None else None


class OAuthState(Base):
    """A short-lived OAuth ``state`` value, persisted so it survives restarts.

    Replaces the in-memory ``state_store`` dict. ``state`` is the anti-CSRF nonce
    minted at ``/auth/start`` and verified at ``/auth/callback``; the row is
    valid until ``expires_at``.
    """

    __tablename__ = "oauth_state"
    # The TTL sweep filters on expires_at; index it so the sweep stays a
    # range scan as the table grows.
    __table_args__ = (Index("ix_oauth_state_expires_at", "expires_at"),)

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    iss: Mapped[str] = mapped_column(String(512))
    provider: Mapped[str] = mapped_column(String(64))
    # Reserved for the PKCE verifier; null until the flow generates one.
    code_verifier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    @classmethod
    def issue(
        cls,
        state: str,
        iss: str,
        provider: str,
        ttl_seconds: int,
        code_verifier: str | None = None,
    ) -> "OAuthState":
        """Mint a state row that expires ``ttl_seconds`` from now."""
        return cls(
            state=state,
            iss=iss,
            provider=provider,
            code_verifier=code_verifier,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )

    @property
    def is_expired(self) -> bool:
        """True once the row has passed its TTL (compared in UTC)."""
        return utcnow() >= self.expires_at


class ProviderToken(Base):
    """Encrypted OAuth tokens for one patient at one provider connection.

    The ``(patient_fhir_id, provider, iss)`` triple is unique so a re-auth
    updates the existing row instead of accumulating duplicates.
    """

    __tablename__ = "provider_token"
    __table_args__ = (
        UniqueConstraint(
            "patient_fhir_id", "provider", "iss", name="uq_provider_token_identity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    patient_fhir_id: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64))
    iss: Mapped[str] = mapped_column(String(512))
    access_token: Mapped[str] = mapped_column(EncryptedString)
    refresh_token: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
