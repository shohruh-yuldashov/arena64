"""A broadcast is committed, not merely staged — A64-027A.

The bug this file exists for: `SessionUnitOfWork.__aexit__` rolls back on an
exception and commits **nothing** on its own — repositories.md §5.1, "exiting
the scope without an explicit commit rolls back". A service that opens the
scope and forgets to ask therefore returns a perfectly good broadcast id from
a `202`, and stores no row at all.

## Why this is a unit test and not a contract one

Every contract assertion reads back through the same session that wrote, so
every one of them passes against this bug — the row is visible inside the
transaction that is about to be discarded. And the contract fixture wraps
each test in a transaction it rolls back, so "did the service commit" is not
expressible there either: an explicit rollback tears down the fixture, and a
second connection cannot see the fixture's own uncommitted setup.

What *is* expressible, precisely, is that the service asked. That is the
whole invariant, and a recording unit of work states it directly.

It was found by signing into the console as a real administrator and sending
a real broadcast: the API answered `202` and
`notifications.notification_broadcast` stayed empty.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.modules.notifications.application.services.broadcast_service import (
    BroadcastRequest,
    BroadcastService,
)
from app.modules.notifications.domain.broadcast import Broadcast, BroadcastAudience


class _RecordingUnitOfWork:
    """A scope that remembers whether it was asked to commit."""

    def __init__(self) -> None:
        self.committed = 0
        self.rolled_back = 0

    async def __aenter__(self) -> "_RecordingUnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is not None:
            self.rolled_back += 1

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1


class _Repository:
    def __init__(self) -> None:
        self.created: list[Broadcast] = []

    async def create(self, broadcast: Broadcast) -> Broadcast:
        self.created.append(broadcast)
        return broadcast


class _Audience:
    async def count_eligible(self) -> int:
        return 7

    async def page_eligible(self, *, after: UUID | None, limit: int) -> list[UUID]:
        return []


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


def _service(unit_of_work: _RecordingUnitOfWork, repository: _Repository) -> BroadcastService:
    return BroadcastService(
        repository=repository,  # type: ignore[arg-type]
        audience=_Audience(),
        clock=_Clock(),
        unit_of_work=unit_of_work,
    )


def _request() -> BroadcastRequest:
    return BroadcastRequest(
        title="Texnik ishlar",
        body="Bugun kechqurun 30 daqiqa.",
        locale="uz",
        audience=BroadcastAudience.ALL_PLAYERS,
        idempotency_key=uuid4().hex,
    )


@pytest.mark.asyncio
async def test_creating_a_broadcast_commits() -> None:
    """Without this the endpoint answers `202` and stores nothing."""
    unit_of_work = _RecordingUnitOfWork()
    repository = _Repository()

    await _service(unit_of_work, repository).create(_request(), created_by=uuid4())

    assert repository.created, "the repository was never asked to store it"
    assert unit_of_work.committed == 1


@pytest.mark.asyncio
async def test_a_failing_write_is_not_committed() -> None:
    """The scope rolls back and nothing claims to have been stored."""

    class _Broken(_Repository):
        async def create(self, broadcast: Broadcast) -> Broadcast:
            raise RuntimeError("the database said no")

    unit_of_work = _RecordingUnitOfWork()

    with pytest.raises(RuntimeError):
        await _service(unit_of_work, _Broken()).create(_request(), created_by=uuid4())

    assert unit_of_work.committed == 0
    assert unit_of_work.rolled_back == 1
