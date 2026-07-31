"""app.database.health.check_database_connection — the failure path,
isolated from any real database via a stub. The success path against a
real database is proven in
tests/contract/test_session_manager.py::TestHealthCheckIntegration; a
contract test can never exercise "the database is unreachable" without
actually taking it down, which is exactly what this unit test is for.
"""

from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.health import check_database_connection


class _RaisingSession:
    async def execute(self, *args: Any, **kwargs: Any) -> None:
        raise ConnectionRefusedError("connection refused")


class _SucceedingSession:
    async def execute(self, *args: Any, **kwargs: Any) -> None:
        return None


class TestCheckDatabaseConnection:
    async def test_returns_false_and_does_not_raise_on_failure(self) -> None:
        session = cast(AsyncSession, _RaisingSession())
        assert await check_database_connection(session) is False

    async def test_returns_true_on_success(self) -> None:
        session = cast(AsyncSession, _SucceedingSession())
        assert await check_database_connection(session) is True
