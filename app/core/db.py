"""Async database engine, session factory, request dependency, and data access.

SQLAlchemy 2.0 async: one ``engine`` owns the connection pool for the process,
``async_sessionmaker`` mints sessions, and ``get_session`` is the FastAPI
dependency that yields one session per request and closes it afterwards. The
sweeps, ``persist_token`` and the connection lookups are the persistence
operations the endpoints build on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.models import (
    AppSession,
    OAuthState,
    ProviderToken,
    expiry_from,
    new_patient_id,
    utcnow,
)
from app.providers.models import TokenSet
from app.core.config import get_settings

# One engine per process. ``pool_pre_ping`` checks a connection before handing it
# out, so a Postgres restart or a dropped idle connection surfaces as a clean
# reconnect rather than a mid-request error.
engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)

# ``expire_on_commit=False`` keeps attributes readable after commit; otherwise the
# ORM would try a fresh (now out-of-context) async load when we read ids back.
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a database session for one request, closing it on the way out."""
    async with SessionFactory() as session:
        yield session


async def _delete_expired(session: AsyncSession, model) -> int:
    """Stage deletion of a TTL model's rows past their expiry; the caller commits.

    Returns the number of rows removed. Compared in UTC to match how
    ``expires_at`` is stored. Both TTL tables sweep the same way, so they share
    this implementation rather than drifting apart.
    """
    result = await session.execute(
        delete(model).where(model.expires_at < utcnow())
    )
    return result.rowcount or 0


async def delete_expired_states(session: AsyncSession) -> int:
    """Stage deletion of OAuth state rows past their TTL; the caller commits."""
    return await _delete_expired(session, OAuthState)


async def delete_expired_sessions(session: AsyncSession) -> int:
    """Stage deletion of app session rows past their TTL; the caller commits."""
    return await _delete_expired(session, AppSession)


async def delete_dead_connections(session: AsyncSession) -> int:
    """Stage deletion of connections nothing can reach or revive; caller commits.

    A connection outlives the session that made it on purpose — that is what a
    stored refresh token buys — so "no live session" cannot be the whole rule or
    the sweep would take exactly the rows worth keeping.

    Two things keep a connection outright. A live session on its record, and its
    own access token for as long as that still works: a token that has not
    expired can serve a read this instant, so a connection holding one is in use
    by definition. That second guard also covers the moment inside an
    authorization between a token being committed and the session that reads it
    being issued, where a rule keyed on reachability alone would delete a
    connection in the act of creating it.

    Past those, what decides it is whether the connection could still be brought
    back to life, and that grades how long it is given rather than whether it is
    taken at all:

    * One holding a refresh token the provider has not refused can be renewed
      whenever a read comes, so its expired access token means nothing on its
      own. It is kept until nothing has read it for the configured retention
      window. Every read stamps ``last_used_at`` across the record, so that
      window only ever runs out on a connection nobody is using.
    * One with no refresh token, or one whose refresh came back ``invalid_grant``
      and so has none any more, was worth exactly its access token and is now
      worth nothing. No window is imposed on top: waiting out a month it cannot
      come back from would only be storing a dead secret for longer.

    Nothing here reaches out to a provider. This runs inside a request, and a
    token endpoint having a bad day must not be able to stall one; revoking at
    the provider belongs on the deliberate path, where a caller is waiting for
    the answer anyway.

    A record has no row of its own, so it disappears with its last connection.
    """
    settings = get_settings()
    now = utcnow()

    reachable = (
        select(AppSession.session_id)
        .where(
            AppSession.patient_id == ProviderToken.patient_id,
            AppSession.expires_at > now,
        )
        .exists()
    )
    # A server that stated no lifetime told us nothing to expire against, so the
    # only honest stand-in is how long a session issued alongside it could have
    # lived. Written as two comparisons rather than arithmetic on the column,
    # which Postgres and SQLite do not spell the same way.
    lapsed = or_(
        ProviderToken.expires_at < now,
        and_(
            ProviderToken.expires_at.is_(None),
            ProviderToken.created_at
            < now - timedelta(seconds=settings.app_session_ttl_seconds),
        ),
    )
    unrevivable = ProviderToken.refresh_token.is_(None)
    idle = (
        func.coalesce(ProviderToken.last_used_at, ProviderToken.created_at)
        < now - timedelta(days=settings.connection_retention_days)
    )

    result = await session.execute(
        delete(ProviderToken).where(
            ~reachable,
            lapsed,
            or_(unrevivable, idle),
        )
    )
    return result.rowcount or 0


# How stale a connection's last-read stamp may get before a read refreshes it.
# The only thing that reads the stamp is the sweep, which measures in days, so
# recording every individual read would buy precision nothing consumes — and it
# would buy it on the read path, where a write means a round trip, a commit, and
# on Postgres a rewritten row plus a new index entry every time a caller polls.
_USE_STAMP_INTERVAL = timedelta(hours=1)


async def mark_record_used(
    session: AsyncSession, connections: list[ProviderToken]
) -> None:
    """Note that this record was reached, unless it says so recently enough.

    Takes the record's connections rather than a narrowed selection of them,
    because what happened is that the record was reached: reading one provider
    does not make the rest of it abandoned. One statement covers however many
    there are.

    Rows already stamped within the interval are left alone, which is what keeps
    a polling caller from turning every poll into a write.
    """
    if not connections:
        return
    stale_from = utcnow() - _USE_STAMP_INTERVAL
    if all(
        connection.last_used_at is not None and connection.last_used_at > stale_from
        for connection in connections
    ):
        return

    await session.execute(
        update(ProviderToken)
        .where(ProviderToken.patient_id == connections[0].patient_id)
        .values(last_used_at=utcnow())
    )
    await session.commit()


async def connections_for_patient(
    session: AsyncSession, patient_id: str
) -> list[ProviderToken]:
    """Every stored connection under one patient record, oldest first.

    Ordered by id so a record reads back in the order its connections were made,
    which keeps a response stable between calls rather than leaving it to the
    database's choice of plan.
    """
    result = await session.execute(
        select(ProviderToken)
        .where(ProviderToken.patient_id == patient_id)
        .order_by(ProviderToken.id)
    )
    return list(result.scalars().all())


async def persist_token(
    session: AsyncSession,
    *,
    patient_id: str | None = None,
    provider: str,
    iss: str,
    token_set: TokenSet,
) -> ProviderToken:
    """Store the token for one connection, under the record it belongs to.

    ``patient_id`` is the record the caller proved they hold, by presenting a
    live session when the authorization started, and is the only thing that may
    place a connection on an existing record. Without one the connection anchors
    a new record even where the same account is already held elsewhere, because
    the server cannot tell this caller from whoever assembled that record — see
    ``ProviderToken``. Within a record the connection is unique, so a re-auth the
    caller does hold updates it in place rather than leaving a stale token.

    Returns the row, so the caller can read back the record it settled on.
    """
    patient_fhir_id = token_set.patient or ""

    token = None
    if patient_id is not None:
        existing = await session.execute(
            select(ProviderToken).where(
                ProviderToken.patient_id == patient_id,
                ProviderToken.patient_fhir_id == patient_fhir_id,
                ProviderToken.provider == provider,
                ProviderToken.iss == iss,
            )
        )
        token = existing.scalar_one_or_none()

    if token is None:
        token = ProviderToken(
            patient_id=patient_id or new_patient_id(),
            patient_fhir_id=patient_fhir_id,
            provider=provider,
            iss=iss,
        )
        session.add(token)

    token.access_token = token_set.access_token
    token.refresh_token = token_set.refresh_token
    token.scope = token_set.scope
    token.expires_at = expiry_from(token_set.expires_in)
    await session.commit()
    return token
