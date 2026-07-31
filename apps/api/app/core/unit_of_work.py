"""The unit-of-work contract — repositories.md §5.1.

Owns the transaction boundary. A service opens exactly one per use case
(services.md §9.1) and passes it to the repositories it needs for that use
case; repositories enlist in it, they never open, commit, or roll it back
themselves (repositories.md §4).

The concrete SQLAlchemy implementation is `app.database.unit_of_work` — kept
out of `core/` because `core/` holds contracts an application layer programs
against, never a driver (dependency-injection.md §3.2).
"""

from types import TracebackType
from typing import Protocol, Self


class UnitOfWork(Protocol):
    """An async context manager around one transaction.

    Exiting the scope without an explicit `commit()` rolls back — fail-safe:
    a forgotten commit loses work loudly instead of committing partial work
    quietly (repositories.md §5.1).
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None:
        """Flush and commit. The *only* method that commits — a repository
        may flush to obtain a generated identity, but only the unit of work
        commits (repositories.md §5.1)."""
        ...

    async def rollback(self) -> None: ...
