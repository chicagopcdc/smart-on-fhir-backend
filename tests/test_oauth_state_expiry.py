"""OAuth state TTL: the is_expired check and the sweep that removes stale rows."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.db import delete_expired_states
from app.auth.models import OAuthState, utcnow


def _state(name: str, *, expires_in: timedelta) -> OAuthState:
    return OAuthState(
        state=name,
        iss="https://example.org/fhir",
        provider="EPIC_SANDBOX",
        expires_at=utcnow() + expires_in,
    )


def test_is_expired_reflects_the_ttl():
    fresh = _state("fresh", expires_in=timedelta(minutes=5))
    stale = _state("stale", expires_in=timedelta(minutes=-1))

    assert fresh.is_expired is False
    assert stale.is_expired is True


async def test_delete_expired_states_removes_only_expired(db_session):
    db_session.add(_state("fresh", expires_in=timedelta(minutes=10)))
    db_session.add(_state("stale", expires_in=timedelta(minutes=-10)))
    await db_session.commit()

    removed = await delete_expired_states(db_session)

    assert removed == 1
    remaining = (await db_session.execute(select(OAuthState.state))).scalars().all()
    assert remaining == ["fresh"]
