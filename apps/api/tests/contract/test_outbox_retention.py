"""`SqlAlchemyOutboxRetentionStore` against real PostgreSQL — A64-014.1.

`tests/unit/test_outbox_retention.py` covers the pruner's arithmetic and
draining over an in-memory store. What it cannot cover is whether the two
`DELETE` statements actually select what they claim to — a fake's `WHERE`
clause is a Python comprehension that agrees with itself.

Three properties, and each of them is a permanent, silent data loss if the
statement is wrong:

    an unpublished entry is never deleted, at any age
    the horizon is applied to `occurred_at`, the partition key
    the ledger's row-wise composite key matches exactly the rows it locked

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.events import DomainEvent
from app.platform.outbox import (
    OutboxEntry,
    OutboxModel,
    ProcessedEventModel,
    SqlAlchemyOutboxRepository,
    SqlAlchemyOutboxRetentionStore,
    SqlAlchemyProcessedEventStore,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
HORIZON = NOW - timedelta(days=14)
CONSUMER = "test_consumer"


@dataclass(frozen=True)
class _Thing(DomainEvent):
    event_type: ClassVar[str] = "test.thing_happened"
    aggregate_type: ClassVar[str] = "thing"

    thing_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.thing_id

    def payload(self) -> dict[str, Any]:
        return {"thing_id": str(self.thing_id)}


@pytest_asyncio.fixture
async def outbox(contract_session: AsyncSession) -> SqlAlchemyOutboxRepository:
    return SqlAlchemyOutboxRepository(contract_session)


@pytest_asyncio.fixture
async def retention(contract_session: AsyncSession) -> SqlAlchemyOutboxRetentionStore:
    return SqlAlchemyOutboxRetentionStore(contract_session)


async def _entry(
    outbox: SqlAlchemyOutboxRepository,
    session: AsyncSession,
    *,
    at: datetime,
    published: bool,
) -> OutboxEntry:
    entry = await outbox.enqueue(OutboxEntry.of(_Thing(occurred_at=at, thing_id=uuid4())))
    await session.flush()
    if published:
        await outbox.mark_published([entry.id], at=at)
    return entry


async def _count(session: AsyncSession, model: type[Any]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


class TestPruningEntries:
    async def test_an_old_published_entry_is_deleted(
        self,
        outbox: SqlAlchemyOutboxRepository,
        retention: SqlAlchemyOutboxRetentionStore,
        contract_session: AsyncSession,
    ) -> None:
        await _entry(outbox, contract_session, at=NOW - timedelta(days=30), published=True)

        deleted = await retention.prune_published(before=HORIZON, batch_size=100)

        assert deleted == 1
        assert await _count(contract_session, OutboxModel) == 0

    async def test_a_recent_published_entry_is_kept(
        self,
        outbox: SqlAlchemyOutboxRepository,
        retention: SqlAlchemyOutboxRetentionStore,
        contract_session: AsyncSession,
    ) -> None:
        """The horizon is applied to `occurred_at`, which is DB-18's
        partition key — so this predicate and a future `DETACH PARTITION`
        select the same rows."""
        await _entry(outbox, contract_session, at=NOW - timedelta(days=1), published=True)

        assert await retention.prune_published(before=HORIZON, batch_size=100) == 0
        assert await _count(contract_session, OutboxModel) == 1

    async def test_an_unpublished_entry_survives_any_age(
        self,
        outbox: SqlAlchemyOutboxRepository,
        retention: SqlAlchemyOutboxRetentionStore,
        contract_session: AsyncSession,
    ) -> None:
        """**The assertion that matters most in this file.** An exhausted
        entry is still owed to somebody, and deleting one destroys an event
        nobody has delivered — silently, permanently, with no recovery
        path."""
        await _entry(outbox, contract_session, at=NOW - timedelta(days=365), published=False)

        assert await retention.prune_published(before=HORIZON, batch_size=100) == 0
        assert await _count(contract_session, OutboxModel) == 1

    async def test_the_delete_is_bounded_by_its_batch(
        self,
        outbox: SqlAlchemyOutboxRepository,
        retention: SqlAlchemyOutboxRetentionStore,
        contract_session: AsyncSession,
    ) -> None:
        """A bare `DELETE ... WHERE occurred_at < cutoff` would take a lock
        proportional to the backlog on the platform's highest-churn
        relation. The limit is what makes the job safe to run at all."""
        for day in range(5):
            await _entry(
                outbox, contract_session, at=NOW - timedelta(days=30 + day), published=True
            )

        assert await retention.prune_published(before=HORIZON, batch_size=2) == 2
        assert await _count(contract_session, OutboxModel) == 3

    async def test_the_oldest_entries_go_first(
        self,
        outbox: SqlAlchemyOutboxRepository,
        retention: SqlAlchemyOutboxRetentionStore,
        contract_session: AsyncSession,
    ) -> None:
        """Oldest-first so the table's floor rises monotonically, which is
        what makes an "oldest retained row" metric mean anything."""
        oldest = await _entry(outbox, contract_session, at=NOW - timedelta(days=60), published=True)
        newer = await _entry(outbox, contract_session, at=NOW - timedelta(days=20), published=True)

        await retention.prune_published(before=HORIZON, batch_size=1)

        assert await outbox.get(oldest.id) is None
        assert await outbox.get(newer.id) is not None


class TestPruningTheLedger:
    async def test_an_old_ledger_row_is_deleted(
        self, retention: SqlAlchemyOutboxRetentionStore, contract_session: AsyncSession
    ) -> None:
        ledger = SqlAlchemyProcessedEventStore(contract_session)
        await ledger.mark_processed(CONSUMER, [uuid4()], at=NOW - timedelta(days=60))
        await contract_session.flush()

        assert await retention.prune_processed_events(before=HORIZON, batch_size=100) == 1
        assert await _count(contract_session, ProcessedEventModel) == 0

    async def test_a_recent_ledger_row_is_kept(
        self, retention: SqlAlchemyOutboxRetentionStore, contract_session: AsyncSession
    ) -> None:
        ledger = SqlAlchemyProcessedEventStore(contract_session)
        await ledger.mark_processed(CONSUMER, [uuid4()], at=NOW - timedelta(days=1))
        await contract_session.flush()

        assert await retention.prune_processed_events(before=HORIZON, batch_size=100) == 0
        assert await _count(contract_session, ProcessedEventModel) == 1

    async def test_the_ledger_delete_is_bounded_by_its_batch(
        self, retention: SqlAlchemyOutboxRetentionStore, contract_session: AsyncSession
    ) -> None:
        """The composite key is matched row-wise, so the `DELETE` removes
        exactly the rows the bounded select locked — a second
        `WHERE processed_at < before` on the delete would be unbounded again
        and the limit would be decorative."""
        ledger = SqlAlchemyProcessedEventStore(contract_session)
        await ledger.mark_processed(
            CONSUMER, [uuid4() for _ in range(5)], at=NOW - timedelta(days=60)
        )
        await contract_session.flush()

        assert await retention.prune_processed_events(before=HORIZON, batch_size=2) == 2
        assert await _count(contract_session, ProcessedEventModel) == 3

    async def test_only_the_matching_consumer_rows_go(
        self, retention: SqlAlchemyOutboxRetentionStore, contract_session: AsyncSession
    ) -> None:
        """The prune is by time and not by consumer, but the key is
        `(consumer, event_id)` — so a row-wise match that dropped the
        consumer half would delete another subscriber's ledger entry for the
        same event."""
        ledger = SqlAlchemyProcessedEventStore(contract_session)
        event_id = uuid4()
        await ledger.mark_processed(CONSUMER, [event_id], at=NOW - timedelta(days=60))
        await ledger.mark_processed("other_consumer", [event_id], at=NOW - timedelta(days=1))
        await contract_session.flush()

        await retention.prune_processed_events(before=HORIZON, batch_size=100)

        assert await _count(contract_session, ProcessedEventModel) == 1
        assert await ledger.unprocessed("other_consumer", [event_id]) == frozenset()


class TestPartitionReadiness:
    async def test_unpublished_rows_past_the_horizon_are_counted(
        self,
        outbox: SqlAlchemyOutboxRepository,
        retention: SqlAlchemyOutboxRetentionStore,
        contract_session: AsyncSession,
    ) -> None:
        """The number that says why the floor did not move, and — once
        DB-18's partitions exist — why the oldest cannot be detached."""
        await _entry(outbox, contract_session, at=NOW - timedelta(days=30), published=False)
        await _entry(outbox, contract_session, at=NOW - timedelta(days=30), published=True)
        await _entry(outbox, contract_session, at=NOW - timedelta(days=1), published=False)

        assert await retention.unpublished_before(HORIZON) == 1

    async def test_a_healthy_platform_counts_none(
        self,
        outbox: SqlAlchemyOutboxRepository,
        retention: SqlAlchemyOutboxRetentionStore,
        contract_session: AsyncSession,
    ) -> None:
        await _entry(outbox, contract_session, at=NOW - timedelta(days=30), published=True)

        assert await retention.unpublished_before(HORIZON) == 0
