"""App session TTL: issuance, the is_expired check, and the expiry sweep.

The app session is the credential the frontend presents to read resources after
a completed authorization; like the OAuth state it is short-lived and swept.
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.db import delete_expired_sessions
from app.auth.models import AppSession


def _session(patient: str, *, ttl_seconds: int) -> AppSession:
    return AppSession.issue(
        patient_fhir_id=patient,
        provider="EPIC_SANDBOX",
        iss="https://example.org/fhir",
        ttl_seconds=ttl_seconds,
    )


def test_issue_mints_an_unguessable_id_and_sets_the_ttl():
    session = _session("p1", ttl_seconds=3600)

    # An opaque, high-entropy id the caller could not have guessed.
    assert session.session_id
    assert len(session.session_id) >= 32
    assert session.is_expired is False


def test_is_expired_reflects_the_ttl():
    assert _session("p1", ttl_seconds=-1).is_expired is True


async def test_delete_expired_sessions_removes_only_expired(db_session):
    db_session.add(_session("fresh", ttl_seconds=600))
    db_session.add(_session("stale", ttl_seconds=-600))
    await db_session.commit()

    removed = await delete_expired_sessions(db_session)

    assert removed == 1
    remaining = (
        await db_session.execute(select(AppSession.patient_fhir_id))
    ).scalars().all()
    assert remaining == ["fresh"]
