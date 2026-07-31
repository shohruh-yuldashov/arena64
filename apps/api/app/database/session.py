"""Per-request session scope — dependency-injection.md §1.4: one unit of
work per request, the request being the natural boundary for the HTTP
entrypoint (a future gateway entrypoint scopes per inbound command instead,
per DI-02, when it exists). Bridged into FastAPI by app/api/deps.py; this
module holds the framework-agnostic factory.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@asynccontextmanager
async def open_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Opens one session for the scope of the `with` block.

    Exiting without an explicit `commit()` rolls back: `AsyncSession.close()`
    — called on context exit — rolls back any open transaction before
    releasing the connection back to the pool. This is repositories.md
    §5.1's fail-safe by construction: a forgotten commit loses work loudly,
    never partially.
    """
    async with session_factory() as session:
        yield session
