"""The concrete units of work — repositories.md §5.1.

Two implementations of `app.core.unit_of_work.UnitOfWork`, differing only
in who owns the session's lifetime:

  `SqlAlchemyUnitOfWork`   opens its own session from a factory. For a
                            caller with no ambient session — a script, a
                            future Celery task, the clock loop.
  `SessionUnitOfWork`       adapts a session someone else already opened.
                            For the HTTP entrypoint, where `api/deps.py`
                            opens exactly one session per request and the
                            service must own the *transaction* without
                            owning the *session*.

Both exist because services.md §9.1 puts the transaction boundary at the
application service method, while dependency-injection.md §1.4 puts the
session scope at the entrypoint. Those are different boundaries that
happen to coincide for HTTP; `SessionUnitOfWork` is what lets a service
commit without ever touching `AsyncSession` itself (services.md §3.3
prohibits exactly that).

The outbox writer will eventually enlist in the same session
(architecture.md AD-16); no outbox exists yet, so there is nothing to
enlist — this is the transaction boundary alone.
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


class SessionUnitOfWork:
    """A transaction boundary over a session this object does not own.

    `__aenter__`/`__aexit__` deliberately do **not** open or close the
    session — whoever opened it (for HTTP, `api/deps.py`'s per-request
    dependency) closes it, and closing it here would pull the connection
    out from under a caller still using it. What this class does own is
    the *outcome*: `commit()` on success, and a rollback on an exception
    escaping the scope, which is the fail-safe repositories.md §5.1
    requires ("exiting the scope without an explicit commit rolls back").
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self._session.rollback()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


class ParticipatingUnitOfWork:
    """A unit of work that **stages** and leaves the commit to its caller.

    `UnitOfWork`, in the sense that matters to a service: it is a scope, it
    rolls back on an exception, and its `commit()` is the point at which the
    service considers its work done. What it does not do is end the database
    transaction — the enclosing unit of work does, and everything staged
    through this one commits with it or with nothing.

    ## What this exists for — A64-022.3 §10

    Composing two application services into one atomic operation, where the
    inner one commits by contract and the outer one must not let it.

    Friend challenge acceptance is the case: `game.MatchCreationUseCase`
    creates a match and commits, because that is correct for every caller
    that only creates a match. Acceptance needs the match **and** the
    challenge transition **and** both sets of events to land together —
    `domain-model.md` §10.3 requires it, and A64-022.3 §10 forbids emulating
    it with compensating cleanup.

    Injecting this rather than changing `MatchCreationService` is what keeps
    that composition from being a special case inside `game`: the service is
    unchanged, its other callers are unchanged, and the *caller* that needs
    to own the transaction says so by handing it a unit of work that does not
    finish one.

    ## Why the rollback still happens

    `__aexit__` rolls back on an exception exactly as `SessionUnitOfWork`
    does. An inner service that fails must not leave half its work staged for
    the outer commit to pick up — and rolling back the shared session
    discards the outer work too, which is the correct outcome: the whole
    operation failed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self._session.rollback()

    async def commit(self) -> None:
        """Flushes, and deliberately does not commit.

        The flush matters: it sends the staged statements so that a
        constraint violation surfaces *here*, inside the inner service's
        scope, rather than at the outer commit where the caller cannot tell
        which statement caused it.
        """
        await self._session.flush()

    async def rollback(self) -> None:
        await self._session.rollback()
