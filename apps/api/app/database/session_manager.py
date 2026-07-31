"""The database session manager — a single object owning the engine and
session factory's lifecycle, in place of the two free functions
(`create_engine`, `create_session_factory`) A64-006 called separately at
startup with no object grouping them.

Grouping them means there is exactly one thing that *is* "the database"
for this process: constructed once in `app_factory.py`'s lifespan, closed
once at shutdown, and offering both a session (the ORM unit of work) and a
raw connection (a health check, a future migration runner) from the one
owner — rather than a health check and a repository each holding their own
opinion about how to get one.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.config.settings import PostgresSettings
from app.database.engine import create_engine, create_session_factory
from app.database.session import open_session


class DatabaseSessionManager:
    """Owns one engine and one session factory for the life of the
    process. `app_factory.py`'s lifespan constructs exactly one of these
    at startup and calls `close()` at shutdown — the same
    construct-once/close-once pairing discipline `lifespan` itself exists
    to enforce.
    """

    def __init__(self, settings: PostgresSettings) -> None:
        self._engine: AsyncEngine = create_engine(settings)
        self._session_factory: async_sessionmaker[AsyncSession] = create_session_factory(
            self._engine
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Exposed for `app.database.unit_of_work.SqlAlchemyUnitOfWork`,
        which takes a factory directly rather than the manager — a unit of
        work is scoped to one use case, not to the manager's process
        lifetime, and should not hold a reference to more than it needs."""
        return self._session_factory

    async def close(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        """A raw connection, outside the ORM session — for a health check
        or anything else that only needs `SELECT 1`, not a unit of work."""
        async with self._engine.connect() as connection:
            yield connection

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """One `AsyncSession` for the scope of the `with` block. Delegates
        to `app.database.session.open_session` rather than reimplementing
        it, so `api/deps.py`'s HTTP-scoped session and a future non-HTTP
        caller (a script, a worker task) share one definition of what
        "opening a session" means.
        """
        async with open_session(self._session_factory) as session:
            yield session
