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


def new_user_id() -> str:
    """Mint an identifier for a signed-in user.

    Ours rather than the identity provider's ``sub``, so that the account keeps
    one identifier even if a deployment moves between IdPs, and so that nothing
    an IdP chose to encode in a subject ends up in our URLs or logs.
    """
    return "usr_" + secrets.token_urlsafe(24)


class Base(DeclarativeBase):
    """Declarative base shared by every model; Alembic reads its metadata."""


class User(Base):
    """A person who has signed in, and the patient record they own.

    Identity comes from an OIDC provider and is keyed on ``(issuer, subject)``,
    which is the only pair OIDC guarantees is stable and unique. Email is
    deliberately *not* the key: a provider may let an address change hands, and
    two providers may both assert the same address for different people. It is
    stored to show the user which account they are signed in as, and for nothing
    that decides access.

    ``patient_id`` is what makes this an authentication layer rather than a
    sign-in page. The record an authorization builds used to be reachable only
    through the session that created it, so a session expiring stranded it and
    the next login began an empty one. Holding the record on the account means
    signing back in returns the same one, with every provider still on it.

    One account, one record: a person has a single medical history however many
    hospitals hold pieces of it, which is the same premise ``ProviderToken``
    already encodes by letting one record span providers. It is null until the
    account's first provider is connected, since a record exists only for as
    long as connections hang off it.
    """

    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_app_user_identity"),
        # Resolving a session to its owner happens on every authenticated
        # request, and the account's record is read from the row it finds.
        Index("ix_app_user_patient_id", "patient_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_user_id)
    # The IdP that vouched for this person, and their identifier at it. Stored
    # together because a subject only means anything beside the issuer that
    # minted it: "1234" at Okta and "1234" at Google are different people.
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))
    # 320 is the longest an email address can be (RFC 3696: 64 local + @ + 255).
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    patient_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LoginState(Base):
    """A login in flight: the nonce and PKCE secret it must come back with.

    The counterpart of ``OAuthState`` for the user-identity flow rather than the
    EHR one. They are kept apart rather than sharing a table because they carry
    different secrets and answer different questions — this one has a ``nonce``,
    which is what binds an ID token to the request that asked for it, and no
    issuer or provider, because there is only ever one IdP configured.

    Keeping them apart also keeps a state minted for one flow from being spent
    on the other, which a shared table would make a matter of remembering to
    check a column.
    """

    __tablename__ = "login_state"
    __table_args__ = (Index("ix_login_state_expires_at", "expires_at"),)

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Echoed in the ID token, and checked against this on the way back: it is
    # what stops an ID token obtained elsewhere from being replayed into this
    # login (OIDC Core §3.1.3.7).
    nonce: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    @classmethod
    def issue(cls, ttl_seconds: int) -> "LoginState":
        """Mint a state, nonce and PKCE verifier that expire ``ttl_seconds`` on."""
        return cls(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(16),
            code_verifier=secrets.token_urlsafe(64),
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )

    @property
    def is_expired(self) -> bool:
        """True once the row has passed its TTL (compared in UTC)."""
        return utcnow() >= self.expires_at


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
    # The account that started this authorization, where one was signed in. It
    # travels with the state for the same reason the link does — this row is the
    # only thing that survives the round trip through the EHR — and it is what
    # lets a first connection be handed to the account that made it, which is
    # the moment a record stops being reachable only through one session.
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
        user_id: str | None = None,
    ) -> "OAuthState":
        """Mint a state row that expires ``ttl_seconds`` from now."""
        return cls(
            state=state,
            iss=iss,
            provider=provider,
            code_verifier=code_verifier,
            link_patient_id=link_patient_id,
            user_id=user_id,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )

    @property
    def is_expired(self) -> bool:
        """True once the row has passed its TTL (compared in UTC)."""
        return utcnow() >= self.expires_at


class AppSession(Base):
    """A short-lived session, handed to the frontend as an opaque bearer token.

    There are two ways to hold one, and the difference is what it is anchored to.

    A session minted by *signing in* names a ``user_id`` and nothing else. What
    it reaches is whatever record that account owns at the moment it is
    presented, read through the user rather than frozen here — so a session
    taken out before the account's first provider was connected reaches that
    record the instant it exists, without having to be reissued.

    A session minted by *completing an authorization* with no one signed in names
    a ``patient_id`` directly. That is the original behaviour and all an
    anonymous caller can be given: with no account to hang the record on, the
    session is the only thing that knows about it, and it is lost when the
    session expires.

    Either way, what the session grants is a record and not a patient id supplied
    by the caller — the difference between reading your own record and reading
    anyone's.

    The provider, issuer and FHIR patient id record the connection that minted
    the session, which is what the deprecated single-connection read path
    resolves. They are null on a session that came from a login, because signing
    in is not a connection to anything.
    """

    __tablename__ = "app_session"
    __table_args__ = (
        Index("ix_app_session_expires_at", "expires_at"),
        Index("ix_app_session_patient_id", "patient_id"),
        # Logging out everywhere, and the sweep that follows an account's record
        # being deleted, both select a user's sessions.
        Index("ix_app_session_user_id", "user_id"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Exactly one of these two is set. user_id is the anchor where the caller
    # signed in; patient_id where they did not and the authorization itself is
    # all the identity there is.
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patient_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patient_fhir_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    iss: Mapped[str | None] = mapped_column(String(512), nullable=True)
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
        user_id: str | None = None,
    ) -> "AppSession":
        """Mint a session for a completed authorization, with a fresh opaque id.

        ``user_id`` is set when the caller was signed in while authorizing. The
        connection details are recorded either way, but on a session that has an
        account the record is resolved through it, so the two cannot drift apart
        once that account connects another provider.
        """
        return cls(
            session_id=secrets.token_urlsafe(32),
            user_id=user_id,
            patient_id=patient_id,
            patient_fhir_id=patient_fhir_id,
            provider=provider,
            iss=iss,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )

    @classmethod
    def for_user(cls, *, user_id: str, ttl_seconds: int) -> "AppSession":
        """Mint a session for a user who has just signed in.

        It names no connection, because signing in is not one. What it reaches is
        read from the account when the session is presented.
        """
        return cls(
            session_id=secrets.token_urlsafe(32),
            user_id=user_id,
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

    That duplication is also why the row needs ``last_used_at``. A connection
    deliberately outlives the session that made it, so nothing else on the row
    says when it stopped being worth keeping, and every abandoned authorization
    would otherwise stay forever.
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
    # Held as Fernet ciphertext and reached through the properties below, which
    # are what every caller uses: no application code handles ciphertext.
    #
    # Decrypting on the attribute rather than in the column type is deliberate.
    # A TypeDecorator's result processor runs while SQLAlchemy is building rows,
    # so a value that will not decrypt fails the whole load: the connection could
    # not be listed, let alone reported as unreadable, and every other connection
    # on the same record went down with it — including through the endpoint for
    # disconnecting the bad one. That is reachable without any corruption, by a
    # key rotation that drops a key still in use. A row whose secret cannot be
    # read is still a row, so it loads, and only reading the secret fails.
    encrypted_access_token: Mapped[str] = mapped_column("access_token", Text)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(
        "refresh_token", Text, nullable=True
    )
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When a read last reached the record this connection belongs to. Null means
    # it never has, which is the state every abandoned authorization is left in
    # and a fact worth keeping rather than flattening into created_at.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    @property
    def access_token(self) -> str | None:
        """The token in the clear. Raises ``TokenEncryptionError`` if no key fits.

        None only on a row that has not been given one yet: the column is not
        nullable, so anything loaded from the database has a value.
        """
        stored = self.encrypted_access_token
        return crypto.decrypt(stored) if stored is not None else None

    @access_token.setter
    def access_token(self, value: str) -> None:
        self.encrypted_access_token = crypto.encrypt(value)

    @property
    def refresh_token(self) -> str | None:
        """The refresh token in the clear, or None where the server issued none."""
        stored = self.encrypted_refresh_token
        return crypto.decrypt(stored) if stored is not None else None

    @refresh_token.setter
    def refresh_token(self, value: str | None) -> None:
        self.encrypted_refresh_token = (
            crypto.encrypt(value) if value is not None else None
        )
