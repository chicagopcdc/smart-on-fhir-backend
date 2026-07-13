"""Shared helpers for driving the live ASGI app against a throwaway database.

The full-flow tests all bind ``main.app`` to a per-test SQLite database and talk
to it through an in-process ASGI transport. Keeping that scaffolding here means
the flow tests share one definition instead of each carrying their own copy.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.auth.models import Base
from app.core.db import get_session

_FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a JSON fixture (e.g. a saved SMART discovery document) by file name."""
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


@asynccontextmanager
async def app_db(url: str):
    """Bind the app to a throwaway database for the duration of a test."""
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = override_get_session
    try:
        yield factory
    finally:
        main.app.dependency_overrides.clear()
        await engine.dispose()


def client() -> httpx.AsyncClient:
    """An HTTP client wired to the app over an in-process ASGI transport."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )
