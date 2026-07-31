"""The concrete unit of work — repositories.md §5.1.

Implements `app.core.unit_of_work.UnitOfWork` over a SQLAlchemy
`AsyncSession`. The outbox writer will eventually enlist in the same
session (architecture.md AD-16); no outbox exists yet, so there is nothing
to enlist — this is the transaction boundary alone.
"""

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SqlAlchemyUnitOfWork:
    """One transaction, opened on `__aenter__`, resolved on `__aexit__`.

    A service constructs one of these per use case (services.md §9.1) and
    passes `.session` to whichever repositories it needs; only this object
    commits (repositories.md §5.1) — a repository may flush to obtain a
    generated identity, but never commits.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("SqlAlchemyUnitOfWork used outside its own scope")
        return self._session

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self.session
        try:
            if exc_type is not None:
                await session.rollback()
        finally:
            await session.close()
            self._session = None

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
