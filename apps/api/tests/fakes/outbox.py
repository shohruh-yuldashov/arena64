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

from collections.abc import Sequence
from datetime import datetime
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

    async def enqueue(self, entry: OutboxEntry) -> OutboxEntry:
        self.entries[entry.id] = entry
        return entry

    async def claim(
        self, *, limit: int, claimed_by: str, now: datetime, max_attempts: int
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
            )
            self.entries[entry.id] = taken
            claimed.append(taken)
        return claimed

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


def _replace(entry: OutboxEntry, **changes: object) -> OutboxEntry:
    """`dataclasses.replace`, spelled out for a frozen entry.

    Imported lazily rather than at module scope purely to keep the fake's
    imports to what its readers need — the entry type and nothing else.
    """
    from dataclasses import replace

    return replace(entry, **changes)  # type: ignore[arg-type]
