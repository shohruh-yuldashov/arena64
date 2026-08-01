"""Database fixtures for contract tests — repositories.md RP-05: "one
contract suite runs against ... every real adapter." These connect to a
real PostgreSQL 17 (`docker/docker-compose.yml`'s `postgres` service,
database `arena64_test` — never `arena64`, which `local` points at) and
are *skipped*, not failed, when that database is unreachable, so `pytest`
still runs cleanly for a contributor without Docker running.

**Transaction-rollback-per-test.** Each test runs inside an outer
transaction that is always rolled back, using SQLAlchemy 2's
`join_transaction_mode="create_savepoint"`: code under test may call
`session.commit()` freely — a real service does, and testing it should
not require pretending otherwise — because `commit()` only releases a
`SAVEPOINT` nested inside the outer transaction this fixture controls.
Nothing a test does outlives the test, without needing to `TRUNCATE`
anything between tests.
"""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.database.models  # noqa: F401 — registers every module's tables on Base.metadata
from app.database.base import Base
from tests.contract._models import ContractWidget  # noqa: F401 — registers the table on import

_TEST_DSN = os.environ.get(
    "CONTRACT_TEST_POSTGRES_DSN",
    "postgresql+asyncpg://arena64:arena64@localhost:5432/arena64_test",
)

#: A64-011.8. Database **15**, never 0-4, which is what `local` points the
#: five Redis roles at (`app/config/settings.py`). A contract test flushes
#: the database it is given, and flushing a developer's live match state
#: because a test picked the same index is the kind of accident that only
#: has to happen once.
_TEST_REDIS_URL = os.environ.get("CONTRACT_TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest_asyncio.fixture
async def contract_redis() -> AsyncIterator[Redis]:
    """A real Redis, flushed before and after each test.

    Skipped, not failed, when unreachable — the same contract the Postgres
    fixtures keep, so `pytest` still runs cleanly for a contributor without
    Docker running.

    Flushed on **both** sides deliberately. Flushing on the way in is what
    makes a test independent of whatever ran before it, including a
    previous run that was interrupted; flushing on the way out keeps a
    developer's `redis-cli` session readable. Rate-limit keys expire on
    their own within an hour, but "within an hour" is not isolation.
    """
    client = Redis.from_url(_TEST_REDIS_URL)

    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 — the point is to skip, not fail
        await client.aclose()
        pytest.skip(
            f"contract tests need a reachable Redis at {_TEST_REDIS_URL!r} "
            f"(see docker/docker-compose.yml): {exc}"
        )

    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture
async def contract_engine() -> AsyncIterator[AsyncEngine]:
    """One engine per **test function**, not per session.

    An `AsyncEngine`'s connection pool holds asyncpg connections bound to
    the event loop they were opened in, and pytest-asyncio gives each
    async test function its own loop by default. A session-scoped engine
    is created in the first test's loop and then handed to every
    subsequent test's *different* loop, which corrupts the pooled
    connection — observed directly while building this suite as
    `InterfaceError: cannot perform operation: another operation is in
    progress` on a plain `SAVEPOINT`, with no code in this file doing
    anything concurrent. Recreating the engine per test costs one extra
    connection setup against a local database and removes the entire bug
    class.
    """
    engine = create_async_engine(_TEST_DSN)

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — the point is to skip, not fail, when unreachable
        await engine.dispose()
        pytest.skip(
            f"contract tests need a reachable PostgreSQL at {_TEST_DSN!r} "
            f"(see docker/docker-compose.yml): {exc}"
        )

    schemas = sorted(
        schema for schema in {table.schema for table in Base.metadata.tables.values()} if schema
    )

    async with engine.begin() as connection:
        # `create_all` creates tables, never schemas — a module's tables
        # live in its own schema (database.md DB-03), so without this the
        # whole suite fails on "schema does not exist".
        for schema in schemas:
            await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        await connection.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        # CASCADE because a schema also holds the enum types SQLAlchemy
        # created for it, and `drop_all` does not always remove those —
        # leaving one behind makes the *next* test run fail with "type
        # already exists". Safe here in a way it would not be in a
        # migration: this database exists only for tests.
        for schema in schemas:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    await engine.dispose()


@pytest_asyncio.fixture
async def contract_session(contract_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One `AsyncSession` per test, bound to a connection whose outer
    transaction this fixture always rolls back on exit."""
    async with contract_engine.connect() as connection:
        await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await connection.rollback()
