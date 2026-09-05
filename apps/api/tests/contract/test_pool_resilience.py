"""What the connection pool does when a backend dies — A64-028.3, P2-1.

A64-028.1 recorded that `pool_pre_ping` was absent and that the cost was
"inference, not measurement". This measures it: a pooled connection is
handed back, its backend is terminated from outside exactly as a PostgreSQL
restart would, and the next requests are counted.

## Why this is a contract test and not a unit test

The failure only exists against a real server. A fake pool cannot have a
backend to kill, and the thing under test is what `asyncpg` and SQLAlchemy
do between them when one goes away.
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config.settings import PostgresSettings
from app.database.engine import create_engine
from tests.contract.conftest import _TEST_DSN

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def executioner() -> AsyncIterator[AsyncEngine]:
    """A second engine, so terminating a backend does not use the pool under
    test to do it."""
    engine = create_async_engine(_TEST_DSN)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _kill_a_pooled_backend(engine: AsyncEngine, executioner: AsyncEngine) -> None:
    """Uses a connection, returns it to the pool, then kills it from outside."""
    async with engine.connect() as connection:
        pid = (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()

    async with executioner.connect() as connection:
        await connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        await connection.commit()


async def _outcomes(engine: AsyncEngine, attempts: int = 3) -> list[str]:
    results: list[str] = []
    for _ in range(attempts):
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            results.append("ok")
        except Exception as error:  # noqa: BLE001 — the outcome is the assertion
            results.append(type(error).__name__)
    return results


def _engine(*, pre_ping: bool) -> AsyncEngine:
    # Through `create_engine`, so this tests the engine the application
    # builds rather than a hand-rolled one that happens to agree with it.
    return create_engine(
        PostgresSettings(
            dsn=SecretStr(_TEST_DSN),
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=pre_ping,
        )
    )


async def test_a_dead_pooled_connection_is_replaced_before_it_is_used(
    executioner: AsyncEngine,
) -> None:
    """The configured behaviour: a database restart costs no failed request."""
    engine = _engine(pre_ping=True)
    try:
        await _kill_a_pooled_backend(engine, executioner)

        assert await _outcomes(engine) == ["ok", "ok", "ok"]
    finally:
        await engine.dispose()


async def test_without_pre_ping_the_first_request_after_a_restart_fails(
    executioner: AsyncEngine,
) -> None:
    """The measurement that decided it — A64-028.1's P2-1, answered.

    Not a test of a setting nobody uses: it is the evidence that the default
    above is worth its `SELECT 1`. SQLAlchemy does recover on its own, which
    is why this is one failed request rather than an outage — and why the
    finding was P2 rather than P1.
    """
    engine = _engine(pre_ping=False)
    try:
        await _kill_a_pooled_backend(engine, executioner)

        outcomes = await _outcomes(engine)
        assert outcomes[0] != "ok", "expected the stale connection to surface as an error"
        assert outcomes[1:] == ["ok", "ok"], "expected SQLAlchemy to recover after one failure"
    finally:
        await engine.dispose()


async def test_the_application_default_is_pre_ping() -> None:
    """A64-028.3's decision, asserted where a future edit would meet it."""
    assert PostgresSettings().pool_pre_ping is True
