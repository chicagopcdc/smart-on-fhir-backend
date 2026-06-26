"""Async database engine, session factory, request dependency, and data access.

SQLAlchemy 2.0 async: one ``engine`` owns the connection pool for the process,
``async_sessionmaker`` mints sessions, and ``get_session`` is the FastAPI
dependency that yields one session per request and closes it afterwards. The
``delete_expired_states`` and ``persist_token`` helpers are the persistence
operations the endpoints build on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.models import OAuthState, ProviderToken, utcnow
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


async def delete_expired_states(session: AsyncSession) -> int:
    """Stage deletion of OAuth state rows past their TTL; the caller commits.

    Returns the number of rows removed. Compared in UTC to match how
    ``expires_at`` is stored.
    """
    result = await session.execute(
        delete(OAuthState).where(OAuthState.expires_at < utcnow())
    )
    return result.rowcount or 0


async def persist_token(
    session: AsyncSession, *, provider: str, iss: str, token_set: TokenSet
) -> ProviderToken:
    """Insert or update the stored token for a patient/provider/issuer.

    The identity triple is unique, so re-authenticating updates the existing row
    rather than leaving a stale token behind.
    """
    patient_fhir_id = token_set.patient or ""
    existing = await session.execute(
        select(ProviderToken).where(
            ProviderToken.patient_fhir_id == patient_fhir_id,
            ProviderToken.provider == provider,
            ProviderToken.iss == iss,
        )
    )
    token = existing.scalar_one_or_none()
    if token is None:
        token = ProviderToken(
            patient_fhir_id=patient_fhir_id, provider=provider, iss=iss
        )
        session.add(token)

    token.access_token = token_set.access_token
    token.refresh_token = token_set.refresh_token
    token.scope = token_set.scope
    token.expires_at = (
        utcnow() + timedelta(seconds=token_set.expires_in)
        if token_set.expires_in is not None
        else None
    )
    await session.commit()
    return token
