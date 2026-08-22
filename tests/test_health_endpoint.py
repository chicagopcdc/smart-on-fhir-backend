"""`GET /health` exists for a supervisor, so both of its answers matter.

A healthcheck that can only say yes is a healthcheck that never fires, so the
second test is the one that earns the endpoint its place. What it is for is in
``app/api/health.py``.
"""

from __future__ import annotations

from sqlalchemy.exc import OperationalError

from app import main
from app.core.db import get_session
from tests.app_harness import app_db as _app_db, client as _client


class _UnreachableStore:
    """A session that fails its first query the way a dropped Postgres does.

    Standing in for the database rather than taking one away: the endpoint's job
    is to turn that exception into an answer, and this is the exception.
    """

    async def execute(self, *_args, **_kwargs):
        raise OperationalError("SELECT 1", None, ConnectionRefusedError("refused"))


async def test_health_reports_a_reachable_database(tmp_path):
    async with _app_db(f"sqlite+aiosqlite:///{tmp_path / 'health.db'}"):
        async with _client() as client:
            response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_health_answers_rather_than_raises_when_the_store_is_gone():
    main.app.dependency_overrides[get_session] = _UnreachableStore
    try:
        async with _client() as client:
            response = await client.get("/health")
    finally:
        main.app.dependency_overrides.clear()

    # 503 is what a container runtime and a load balancer act on; the body is
    # for whoever goes looking afterwards. A 500 here would say the healthcheck
    # itself broke, which is a different and much less useful thing to report.
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "error"
