"""Async database engine, session factory, request dependency, and data access.

SQLAlchemy 2.0 async: one ``engine`` owns the connection pool for the process,
``async_sessionmaker`` mints sessions, and ``get_session`` is the FastAPI
dependency that yields one session per request and closes it afterwards. The
``delete_expired_states`` and ``persist_token`` helpers are the persistence
operations the endpoints build on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from sqlalchemy import delete, select
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
