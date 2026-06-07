"""Async database engine, session factory, and the per-request session dependency.

SQLAlchemy 2.0 async: one ``engine`` owns the connection pool for the process,
``async_sessionmaker`` mints sessions, and ``get_session`` is the FastAPI
dependency that yields one session per request and closes it afterwards.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from models import OAuthState, utcnow
from settings import get_settings

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
    """Delete OAuth state rows whose TTL has passed; return the count removed.

    A best-effort sweep called opportunistically from ``/auth/start`` so expired
    anti-CSRF state does not accumulate. The comparison runs in UTC to match how
    ``expires_at`` is stored.
    """
    result = await session.execute(
        delete(OAuthState).where(OAuthState.expires_at < utcnow())
    )
    await session.commit()
    return result.rowcount or 0
