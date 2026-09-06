"""In-memory stand-ins for the outbox's ports — A64-013.7.

What is faked here is **storage and transport**, never the thing under test.
`OutboxRelay` runs for real against these, so the claim/route/record
sequencing, the idempotency filter, the per-entry failure handling and the
backoff arithmetic are all genuinely exercised.

The one deliberate simplification is concurrency: `InMemoryOutbox.claim`
returns due entries in order and marks them claimed, which models
`SKIP LOCKED`'s *effect* for one worker and not its behaviour under two.
That property is a property of PostgreSQL rather than of this code, so it is
asserted where it can be — `tests/contract/test_outbox_repository.py`, with
two real sessions — for the same reason `tests/fakes/rate_limiter.py` does
not reimplement the limiter's Lua.
"""

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

from app.platform.outbox.entry import OutboxEntry


class InMemoryOutbox:
    """The outbox table, as a dict.

    Entries are stored as the frozen `OutboxEntry` values the repository
    returns, and every mutation replaces one — so a test holding a reference
    to a claimed entry keeps seeing what it claimed, exactly as it would with
    the real adapter's mapped-and-detached values.
    """

    def __init__(self) -> None:
        self.entries: dict[UUID, OutboxEntry] = {}
        #: Every `claim` call's `claimed_by`, in order. Asserted by the tests
        #: that care whether the relay claimed at all.
        self.claims: list[str] = []
        #: Every `lock_in_order` call's ids, **as the relay asked for
        #: them**. Recorded unsorted on purpose: the relay's side of the
        #: contract is asking for every entry it is about to write, and
        #: a fake that tidied the list could not tell whether it had.
        self.locked_in_order: list[list[UUID]] = []

    async def enqueue(self, entry: OutboxEntry) -> OutboxEntry:
        self.entries[entry.id] = entry
        return entry

    async def claim(
        self,
        *,
        limit: int,
        claimed_by: str,
        now: datetime,
        max_attempts: int,
        lease: timedelta,
    ) -> Sequence[OutboxEntry]:
        self.claims.append(claimed_by)
        due = sorted(
            (
                entry
                for entry in self.entries.values()
                if entry.published_at is None
                and entry.attempt_count < max_attempts
                and (entry.next_attempt_at is None or entry.next_attempt_at <= now)
            ),
            key=lambda entry: (entry.occurred_at, entry.id),
        )[:limit]

        claimed = []
        for entry in due:
            # The counter increments at claim time, like the real adapter —
            # a relay that dies mid-handler must still burn an attempt.
            taken = _replace(
                entry,
                attempt_count=entry.attempt_count + 1,
                claimed_at=now,
                claimed_by=claimed_by,
                # The lease, like the real adapter — a claimed entry is not
                # due again until it expires, which is what stops a second
                # relay claiming it a poll later (P2-9).
                next_attempt_at=now + lease,
            )
            self.entries[entry.id] = taken
            claimed.append(taken)
        return claimed

    async def lock_in_order(self, entry_ids: Sequence[UUID]) -> None:
        """Records what the relay asked to lock.

        Nothing in memory can deadlock, so the orderedness of the real
        statement is proven against a real database in
        `tests/contract/test_outbox_repository.py`. What is checkable here
        is the half that lives in the relay: that it asks for every entry
        of the tick, once, before writing any of them.
        """
        self.locked_in_order.append(list(entry_ids))

    async def mark_published(self, entry_ids: Sequence[UUID], *, at: datetime) -> int:
        published = 0
        for entry_id in entry_ids:
            entry = self.entries.get(entry_id)
            if entry is None or entry.published_at is not None:
                continue
            self.entries[entry_id] = _replace(
                entry, published_at=at, claimed_at=None, claimed_by=None, last_error=None
            )
            published += 1
        return published

    async def mark_failed(self, entry_id: UUID, *, error: str, retry_at: datetime) -> None:
        entry = self.entries[entry_id]
        self.entries[entry_id] = _replace(
            entry, last_error=error, next_attempt_at=retry_at, claimed_at=None, claimed_by=None
        )

    async def get(self, entry_id: UUID) -> OutboxEntry | None:
        return self.entries.get(entry_id)


class InMemoryProcessedEvents:
    """The `(consumer, event_id)` ledger, as a set."""

    def __init__(self) -> None:
        self.records: set[tuple[str, UUID]] = set()

    async def unprocessed(self, consumer: str, event_ids: Sequence[UUID]) -> frozenset[UUID]:
        return frozenset(
            event_id for event_id in event_ids if (consumer, event_id) not in self.records
        )

    async def mark_processed(
        self, consumer: str, event_ids: Sequence[UUID], *, at: datetime
    ) -> None:
        self.records.update((consumer, event_id) for event_id in event_ids)


class NullUnitOfWork:
    """A transaction boundary over nothing.

    The fakes above have no transaction to commit, and the relay's
    correctness under these tests is about *sequencing* rather than about
    atomicity — which is asserted against real PostgreSQL in the contract
    suite. Counting commits is still useful, because "the claim commits
    before any handler runs" is a claim this class can check.
    """

    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class SingleUseUnitOfWork(NullUnitOfWork):
    """A unit of work that refuses to be entered twice at once.

    The one behaviour of a real `AsyncSession` that matters for the relay's
    concurrency and that `NullUnitOfWork` does not model: a session is a
    single connection, and two coroutines inside it at the same moment
    interleave statements — which asyncpg refuses and SQLAlchemy reports as
    `IllegalStateChangeError`.

    That gap is why A64-020.5F's defect reached production: the relay
    dispatched its consumers under `asyncio.gather` while bracketing each
    with two blocks on **one** shared session, and every unit test drove it
    with a fake that did not care.

    Raises rather than counting, because the failure being modelled is not
    a statistic — it is an operation that cannot happen.

    **It suspends where a real session does.** Without that the bug hides:
    the in-memory ledger fake never awaits anything real, so a whole
    `async with` block runs to completion without the event loop switching
    and two coroutines never actually overlap. A real session round-trips
    to PostgreSQL on entry and on commit, and it is in those windows that
    the interleaving happens.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = False

    async def __aenter__(self) -> Self:
        if self.entered:
            raise RuntimeError(
                "two coroutines are inside one session at once; a real "
                "AsyncSession would raise IllegalStateChangeError here"
            )
        self.entered = True
        await asyncio.sleep(0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.entered = False

    async def commit(self) -> None:
        await asyncio.sleep(0)
        await super().commit()


def _replace(entry: OutboxEntry, **changes: object) -> OutboxEntry:
    """`dataclasses.replace`, spelled out for a frozen entry.

    Imported lazily rather than at module scope purely to keep the fake's
    imports to what its readers need — the entry type and nothing else.
    """
    from dataclasses import replace

    return replace(entry, **changes)  # type: ignore[arg-type]
