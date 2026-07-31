"""`app.database.session_manager.DatabaseSessionManager`, against real
PostgreSQL."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text

from app.config.settings import PostgresSettings
from app.database.health import check_database_connection
from app.database.session_manager import DatabaseSessionManager
from tests.contract.conftest import _TEST_DSN


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[DatabaseSessionManager]:
    settings = PostgresSettings(dsn=_TEST_DSN)
    db = DatabaseSessionManager(settings)
    yield db
    await db.close()


class TestSession:
    async def test_session_executes_a_query(self, manager: DatabaseSessionManager) -> None:
        async with manager.session() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1

    async def test_two_sessions_are_independent(self, manager: DatabaseSessionManager) -> None:
        async with manager.session() as first, manager.session() as second:
            assert first is not second


class TestConnect:
    async def test_connect_yields_a_raw_connection(self, manager: DatabaseSessionManager) -> None:
        async with manager.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            assert result.scalar() == 1


class TestHealthCheckIntegration:
    async def test_check_database_connection_succeeds_against_a_live_database(
        self, manager: DatabaseSessionManager
    ) -> None:
        async with manager.session() as session:
            assert await check_database_connection(session) is True


class TestClose:
    async def test_close_disposes_the_engine(self, manager: DatabaseSessionManager) -> None:
        await manager.close()
        # A second close must not raise — `app_factory.py`'s shutdown path
        # calls this exactly once, but the method itself should be safe to
        # call on an already-disposed engine (SQLAlchemy's own contract).
        await manager.close()
