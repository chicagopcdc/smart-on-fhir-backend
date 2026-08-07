"""Database models: persisted OAuth state and encrypted provider tokens.

Typed SQLAlchemy 2.0 ORM. Timestamps are UTC and stored naive (no tzinfo) so the
models behave identically on Postgres (production) and SQLite (the offline test
engine), which disagree on how they round-trip timezone-aware values.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core import crypto


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (value is UTC, tzinfo stripped)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def expiry_from(expires_in: int | None) -> datetime | None:
    """An absolute expiry from an OAuth ``expires_in``, or None where unstated.

    A token response carries a lifetime in seconds; a stored token needs a point
    in time, since it is read back long after the response arrived. RFC 6749 only
    recommends ``expires_in``, so a server may leave it out, and None is what
    "this server did not say" looks like on the row.
    """
    return utcnow() + timedelta(seconds=expires_in) if expires_in is not None else None


def new_patient_id() -> str:
    """Mint an identifier for a patient record; see ``ProviderToken`` for why.

    The prefix tells it apart at a glance from a FHIR patient id, since both
    travel through this application.
    """
    return "pat_" + secrets.token_urlsafe(24)


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
    minted at ``/auth/connect`` and verified at ``/auth/callback``; the row is
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
    # The patient record this authorization should join, when the caller started
    # it holding a session for one. Null means the connection anchors a record of
    # its own. It is carried here rather than sent back through the browser
    # because this row is the only thing that survives between starting the
    # authorization and the provider redirecting back, and because a link fixed
    # at the start cannot be pointed at another record once the flow completes.
    link_patient_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
        link_patient_id: str | None = None,
    ) -> "OAuthState":
        """Mint a state row that expires ``ttl_seconds`` from now."""
        return cls(
            state=state,
            iss=iss,
            provider=provider,
            code_verifier=code_verifier,
            link_patient_id=link_patient_id,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )

    @property
    def is_expired(self) -> bool:
        """True once the row has passed its TTL (compared in UTC)."""
        return utcnow() >= self.expires_at


class AppSession(Base):
    """A short-lived session issued after a completed authorization.

    Handed to the frontend as an opaque bearer token and presented on resource
    requests. ``patient_id`` is what it grants access to: every connection under
    that record, so a caller reaches only the patient they authenticated as
    rather than a patient id they supply — the difference between reading your
    own record and reading anyone's.

    The provider, issuer and FHIR patient id record the connection that minted
    the session. They are what the single-connection read path resolves, and
    they are how a session is matched back to its connection.
    """

    __tablename__ = "app_session"
    __table_args__ = (
        Index("ix_app_session_expires_at", "expires_at"),
        Index("ix_app_session_patient_id", "patient_id"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64))
    patient_fhir_id: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64))
    iss: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    @classmethod
    def issue(
        cls,
        *,
        patient_id: str,
        patient_fhir_id: str,
        provider: str,
        iss: str,
        ttl_seconds: int,
    ) -> "AppSession":
        """Mint a session with a fresh opaque id that expires after the TTL."""
        return cls(
            session_id=secrets.token_urlsafe(32),
            patient_id=patient_id,
            patient_fhir_id=patient_fhir_id,
            provider=provider,
            iss=iss,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )

    @property
    def is_expired(self) -> bool:
        """True once the row has passed its TTL (compared in UTC)."""
        return utcnow() >= self.expires_at


class ProviderToken(Base):
    """Encrypted OAuth tokens for one patient at one provider connection.

    A row is a *connection*: this person, at this provider, on this server, held
    under one patient record. ``patient_id`` names that record and is what makes
    two connections the same person — asserted by whoever authorizes the second
    provider while holding a session for the first, never inferred from
    ``patient_fhir_id``, which two unrelated servers could spell the same way.

    Uniqueness includes ``patient_id``, so the same EHR account may be held under
    two records, each with its own token. The duplication is the isolation:
    authenticating to a connection proves control of that connection alone, so it
    must not land the caller on a record someone else assembled around it.
    """

    __tablename__ = "provider_token"
    __table_args__ = (
        UniqueConstraint(
            "patient_id",
            "patient_fhir_id",
            "provider",
            "iss",
            name="uq_provider_token_record_identity",
        ),
        # Reading a record fans out over every connection under one patient id,
        # so that lookup is the hot path rather than a scan.
        Index("ix_provider_token_patient_id", "patient_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    patient_id: Mapped[str] = mapped_column(String(64))
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
