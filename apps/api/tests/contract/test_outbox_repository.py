"""`SqlAlchemyOutboxRepository` against real PostgreSQL — A64-013.7.

`tests/unit/test_outbox_relay.py` covers the relay's sequencing over
in-memory storage. What it cannot cover is the one property the outbox is
designed around and that only a real database has:

    SELECT ... FOR UPDATE SKIP LOCKED

Two workers must receive **disjoint** sets. That is what "design for future
horizontal workers" means in practice, it is the difference between
at-least-once and at-least-twice-per-tick, and it is unfalsifiable against a
dictionary — so it is asserted here with two concurrent sessions and two
real transactions.

The rest of the file is the mapping and the predicates: `jsonb` round trips,
the partial index's conditions actually exclude what they claim to, and the
`ON CONFLICT DO NOTHING` on the ledger tolerates the redelivery that
produces it.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.platform.events import DomainEvent
from app.platform.outbox import (
    OutboxEntry,
    OutboxModel,
    SqlAlchemyOutboxRepository,
    SqlAlchemyProcessedEventStore,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
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
        return {"thing_id": str(self.thing_id), "count": 3, "nested": {"ok": True}}


@pytest_asyncio.fixture
async def outbox(contract_session: AsyncSession) -> SqlAlchemyOutboxRepository:
    return SqlAlchemyOutboxRepository(contract_session)


@pytest_asyncio.fixture
async def ledger(contract_session: AsyncSession) -> SqlAlchemyProcessedEventStore:
    return SqlAlchemyProcessedEventStore(contract_session)


async def _enqueue(
    outbox: SqlAlchemyOutboxRepository, *, at: datetime | None = None
) -> OutboxEntry:
    return await outbox.enqueue(OutboxEntry.of(_Thing(occurred_at=at or NOW, thing_id=uuid4())))


class TestEnqueue:
    async def test_an_entry_round_trips_through_the_table(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        entry = await _enqueue(outbox)
        await contract_session.flush()

        stored = await outbox.get(entry.id)
        assert stored is not None
        assert stored.event_type == "test.thing_happened"
        assert stored.aggregate_type == "thing"
        assert stored.event_version == 1

    async def test_the_payload_survives_jsonb(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        """Nested objects, integers and booleans, not only strings. A payload
        the driver quietly stringifies would round trip *differently* rather
        than fail, which is the failure mode a `text` column would have."""
        entry = await _enqueue(outbox)
        await contract_session.flush()
        contract_session.expunge_all()

        stored = await outbox.get(entry.id)
        assert stored is not None
        assert stored.payload["count"] == 3
        assert stored.payload["nested"] == {"ok": True}

    async def test_a_new_entry_is_pending_and_unattempted(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        entry = await _enqueue(outbox)
        await contract_session.flush()

        stored = await outbox.get(entry.id)
        assert stored is not None
        assert stored.published_at is None
        assert stored.attempt_count == 0
        assert stored.claimed_by is None


class TestClaim:
    async def test_a_pending_entry_is_claimed(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        entry = await _enqueue(outbox)
        await contract_session.flush()

        claimed = await outbox.claim(limit=10, claimed_by="w1", now=NOW, max_attempts=5)

        assert [item.id for item in claimed] == [entry.id]
        assert claimed[0].claimed_by == "w1"
        assert claimed[0].attempt_count == 1

    async def test_claims_arrive_in_causation_order(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        """`ORDER BY occurred_at, id` — database.md §12.5. Inserted newest
        first so insertion order cannot be what makes this pass."""
        later = await _enqueue(outbox, at=NOW + timedelta(seconds=5))
        earlier = await _enqueue(outbox, at=NOW)
        await contract_session.flush()

        claimed = await outbox.claim(limit=10, claimed_by="w1", now=NOW, max_attempts=5)

        assert [item.id for item in claimed] == [earlier.id, later.id]

    async def test_a_published_entry_is_not_claimed(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        entry = await _enqueue(outbox)
        await contract_session.flush()
        await outbox.mark_published([entry.id], at=NOW)

        assert not await outbox.claim(limit=10, claimed_by="w1", now=NOW, max_attempts=5)

    async def test_an_entry_backing_off_is_not_claimed(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        entry = await _enqueue(outbox)
        await contract_session.flush()
        await outbox.mark_failed(entry.id, error="boom", retry_at=NOW + timedelta(seconds=30))

        assert not await outbox.claim(limit=10, claimed_by="w1", now=NOW, max_attempts=5)

    async def test_the_entry_returns_once_its_backoff_has_elapsed(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        entry = await _enqueue(outbox)
        await contract_session.flush()
        await outbox.mark_failed(entry.id, error="boom", retry_at=NOW + timedelta(seconds=30))

        claimed = await outbox.claim(
            limit=10, claimed_by="w1", now=NOW + timedelta(seconds=31), max_attempts=5
        )

        assert [item.id for item in claimed] == [entry.id]

    async def test_an_exhausted_entry_stops_being_claimed(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        """`attempt_count >= max_attempts`. The row stays unpublished and
        therefore stays in `ix_outbox__unpublished`, which is what keeps a
        permanently failing event visible in the backlog metric."""
        await _enqueue(outbox)
        await contract_session.flush()

        await outbox.claim(limit=10, claimed_by="w1", now=NOW, max_attempts=2)
        await outbox.claim(limit=10, claimed_by="w1", now=NOW, max_attempts=2)

        assert not await outbox.claim(limit=10, claimed_by="w1", now=NOW, max_attempts=2)

    async def test_the_claim_is_bounded_by_the_limit(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        for offset in range(4):
            await _enqueue(outbox, at=NOW + timedelta(seconds=offset))
        await contract_session.flush()

        claimed = await outbox.claim(limit=2, claimed_by="w1", now=NOW, max_attempts=5)

        assert len(claimed) == 2


class TestConcurrentWorkers:
    """`SKIP LOCKED`, with two real transactions. The property that makes the
    relay horizontally scalable, and the only one a dictionary cannot model.

    Runs off `contract_engine` rather than `contract_session`: that fixture
    binds its session to one connection inside an outer transaction it always
    rolls back, so a `commit()` there releases a savepoint and is invisible
    to any other connection — which is precisely the visibility this test is
    about. Rows are therefore committed for real and deleted in `finally`.
    """

    async def test_two_workers_claim_disjoint_sets(self, contract_engine: AsyncEngine) -> None:
        """The first worker's uncommitted claim locks its rows; the second
        skips them rather than waiting on them or duplicating them.

        Waiting would make N relays one relay with extra latency; duplicating
        would deliver everything twice on every tick rather than only after a
        crash.
        """
        ids: list[UUID] = []
        try:
            async with AsyncSession(contract_engine, expire_on_commit=False) as seeding:
                repository = SqlAlchemyOutboxRepository(seeding)
                for offset in range(4):
                    entry = await _enqueue(repository, at=NOW + timedelta(seconds=offset))
                    ids.append(entry.id)
                await seeding.commit()

            async with (
                AsyncSession(contract_engine, expire_on_commit=False) as first_session,
                AsyncSession(contract_engine, expire_on_commit=False) as second_session,
            ):
                first = await SqlAlchemyOutboxRepository(first_session).claim(
                    limit=2, claimed_by="w1", now=NOW, max_attempts=5
                )
                # The first session has **not** committed, so its two rows
                # are still locked when the second worker polls.
                second = await SqlAlchemyOutboxRepository(second_session).claim(
                    limit=2, claimed_by="w2", now=NOW, max_attempts=5
                )
                await first_session.rollback()
                await second_session.rollback()

            taken_first = {entry.id for entry in first}
            taken_second = {entry.id for entry in second}

            assert len(taken_first) == 2
            assert len(taken_second) == 2
            assert taken_first.isdisjoint(taken_second)
            assert taken_first | taken_second == set(ids)
        finally:
            await _cleanup(contract_engine, ids)


class TestPublication:
    async def test_marking_published_stamps_and_clears_the_claim(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        entry = await _enqueue(outbox)
        await contract_session.flush()
        await outbox.claim(limit=10, claimed_by="w1", now=NOW, max_attempts=5)

        published = await outbox.mark_published([entry.id], at=NOW)

        stored = await outbox.get(entry.id)
        assert published == 1
        assert stored is not None
        assert stored.published_at is not None
        # Cleared, so a stale claim is not left to be misread as "stuck".
        assert stored.claimed_by is None

    async def test_publishing_an_already_published_entry_counts_nothing(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        """The predicate carries `published_at IS NULL` as well as the id
        list, so a row another worker published between this worker's claim
        and its commit is not re-stamped with a later instant."""
        entry = await _enqueue(outbox)
        await contract_session.flush()
        await outbox.mark_published([entry.id], at=NOW)

        assert await outbox.mark_published([entry.id], at=NOW + timedelta(hours=1)) == 0

    async def test_publishing_nothing_issues_no_statement(
        self, outbox: SqlAlchemyOutboxRepository
    ) -> None:
        """`UPDATE ... WHERE id IN ()` is a statement issued to change
        nothing, on the platform's highest-churn relation."""
        assert await outbox.mark_published([], at=NOW) == 0

    async def test_a_failure_is_recorded_on_the_row(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        entry = await _enqueue(outbox)
        await contract_session.flush()

        await outbox.mark_failed(entry.id, error="ConnectionError", retry_at=NOW)

        stored = await outbox.get(entry.id)
        assert stored is not None
        assert stored.last_error == "ConnectionError"
        assert stored.next_attempt_at == NOW

    async def test_a_long_failure_reason_is_truncated(
        self, outbox: SqlAlchemyOutboxRepository, contract_session: AsyncSession
    ) -> None:
        """A driver dumping a whole query into `str(error)` must not turn one
        bad event into a wide row on the highest-churn relation."""
        entry = await _enqueue(outbox)
        await contract_session.flush()

        await outbox.mark_failed(entry.id, error="x" * 5000, retry_at=NOW)

        stored = await outbox.get(entry.id)
        assert stored is not None and stored.last_error is not None
        assert len(stored.last_error) == 500


class TestProcessedEventLedger:
    async def test_an_unrecorded_event_is_reported_unprocessed(
        self, ledger: SqlAlchemyProcessedEventStore
    ) -> None:
        event_id = uuid4()

        assert await ledger.unprocessed(CONSUMER, [event_id]) == frozenset({event_id})

    async def test_a_recorded_event_is_filtered_out(
        self, ledger: SqlAlchemyProcessedEventStore, contract_session: AsyncSession
    ) -> None:
        event_id = uuid4()
        await ledger.mark_processed(CONSUMER, [event_id], at=NOW)
        await contract_session.flush()

        assert await ledger.unprocessed(CONSUMER, [event_id]) == frozenset()

    async def test_the_ledger_is_per_consumer(
        self, ledger: SqlAlchemyProcessedEventStore, contract_session: AsyncSession
    ) -> None:
        """A second subscriber must neither inherit the first's history nor
        skip its own — the key is `(consumer, event_id)`."""
        event_id = uuid4()
        await ledger.mark_processed(CONSUMER, [event_id], at=NOW)
        await contract_session.flush()

        assert await ledger.unprocessed("another_consumer", [event_id]) == frozenset({event_id})

    async def test_recording_the_same_event_twice_is_not_an_error(
        self, ledger: SqlAlchemyProcessedEventStore, contract_session: AsyncSession
    ) -> None:
        """`ON CONFLICT DO NOTHING`, and it is load-bearing: the crash window
        between handling and recording is exactly what produces a
        redelivery, so the redelivery's ledger write must not be the thing
        that fails."""
        event_id = uuid4()
        await ledger.mark_processed(CONSUMER, [event_id], at=NOW)
        await contract_session.flush()

        await ledger.mark_processed(CONSUMER, [event_id], at=NOW + timedelta(hours=1))
        await contract_session.flush()

        assert await ledger.unprocessed(CONSUMER, [event_id]) == frozenset()

    async def test_a_batch_is_one_statement_and_one_answer(
        self, ledger: SqlAlchemyProcessedEventStore, contract_session: AsyncSession
    ) -> None:
        """Batched because the relay claims a page per tick, and one
        `SELECT` per event would make the idempotency check the N+1 the
        batch exists to avoid."""
        seen, unseen = uuid4(), uuid4()
        await ledger.mark_processed(CONSUMER, [seen], at=NOW)
        await contract_session.flush()

        assert await ledger.unprocessed(CONSUMER, [seen, unseen]) == frozenset({unseen})

    async def test_an_empty_batch_asks_nothing(self, ledger: SqlAlchemyProcessedEventStore) -> None:
        """The relay routinely ticks with nothing to do."""
        assert await ledger.unprocessed(CONSUMER, []) == frozenset()


async def _cleanup(engine: AsyncEngine, entry_ids: list[UUID]) -> None:
    """Removes rows `TestConcurrentWorkers` committed.

    Only that test needs it: it has to commit for the lock to be visible
    across connections, and therefore escapes the session fixture's
    rollback. Everything else in this file stays inside it.
    """
    if not entry_ids:
        return

    async with AsyncSession(engine) as session:
        await session.execute(delete(OutboxModel).where(OutboxModel.id.in_(entry_ids)))
        await session.commit()


class TestLockOrderingUnderConcurrency:
    """The deadlock two relays actually hit — A64-028.5A §26.

    A matchmaking burst across two instances produced
    `DeadlockDetectedError` three times on each of them. PostgreSQL breaks a
    cycle by killing one side, and the side it killed was recording work it
    had already delivered: every kill counted as another failed attempt, and
    809 presence events reached the five-attempt ceiling and were abandoned.

    The cycle came from a tick writing its rows in two shapes — successes as
    one batched update, failures one at a time — so two relays with
    overlapping claims took the same locks in opposite orders. Overlapping
    claims are ordinary rather than exotic: a lease lapses while a slow
    handler is still running and the row becomes claimable again.

    Runs off `contract_engine` for the reason `TestConcurrentWorkers` gives:
    two transactions have to be able to see each other.

    What this proves and what it does not: removing `lock_in_order`
    reproduces `DeadlockDetectedError` here, so the single ordered lock
    statement is load-bearing. Removing only its `ORDER BY` does not fail
    this test, because two sessions asking for the *same* two rows are
    served in the same order anyway. The ordering earns its place when
    claims overlap only partly, which two rows cannot express.
    """

    async def test_two_transactions_writing_in_opposite_orders_do_not_deadlock(
        self, contract_engine: AsyncEngine
    ) -> None:
        """Reproduces the cycle exactly, and requires it to resolve.

        The interleaving is forced rather than hoped for. Two coroutines on
        one event loop hand control over only at their own await points, and
        left to themselves the first transaction runs to its commit before
        the second begins — which is why an earlier version of this test
        passed against the unfixed code and proved nothing. The barriers
        below hold each session at the exact point the deadlock needs:
        both have taken one row, and each then reaches for the other's.

        Without `lock_in_order` that is a cycle and PostgreSQL kills one
        side with `DeadlockDetectedError`. With it, both sessions ask for
        both rows in ascending id order before writing either, so the
        second waits for the first instead of dying.
        """
        ids: list[UUID] = []
        try:
            async with AsyncSession(contract_engine, expire_on_commit=False) as seeding:
                repository = SqlAlchemyOutboxRepository(seeding)
                for offset in range(2):
                    entry = await _enqueue(repository, at=NOW + timedelta(seconds=offset))
                    ids.append(entry.id)
                await seeding.commit()

            ordered = sorted(ids)
            first_holds_a_row = asyncio.Event()
            second_holds_a_row = asyncio.Event()

            async def first() -> None:
                async with AsyncSession(contract_engine, expire_on_commit=False) as session:
                    repository = SqlAlchemyOutboxRepository(session)
                    await repository.lock_in_order(ids)
                    await repository.mark_published([ordered[0]], at=NOW)
                    first_holds_a_row.set()

                    # Waits for the other side to take the row it is about
                    # to reach for — the cycle needs both halves to exist.
                    # The timeout is not a fallback but the *fixed*
                    # behaviour: the second session is queued behind this
                    # one's locks and correctly makes no progress at all.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(second_holds_a_row.wait(), timeout=2)

                    await repository.mark_failed(
                        ordered[1], error="Refused", retry_at=NOW + timedelta(minutes=1)
                    )
                    await session.commit()

            async def second() -> None:
                # Starts only once the first session holds a row, so the
                # interleaving is forced rather than hoped for. Two
                # coroutines on one event loop otherwise run one
                # transaction to its commit before starting the other,
                # which is why a version of this test without these events
                # passed against the unfixed code and proved nothing.
                await first_holds_a_row.wait()
                async with AsyncSession(contract_engine, expire_on_commit=False) as session:
                    repository = SqlAlchemyOutboxRepository(session)
                    await repository.lock_in_order(ids)
                    await repository.mark_published([ordered[1]], at=NOW)
                    second_holds_a_row.set()
                    await repository.mark_failed(
                        ordered[0], error="Refused", retry_at=NOW + timedelta(minutes=1)
                    )
                    await session.commit()

            # Opposite orders on purpose: this is the shape that deadlocked.
            # A `DeadlockDetectedError` from either side fails the test by
            # propagating; the timeout catches the other failure mode, a
            # cycle that hangs instead of being detected.
            await asyncio.wait_for(asyncio.gather(first(), second()), timeout=30)
        finally:
            async with AsyncSession(contract_engine) as cleanup:
                await cleanup.execute(delete(OutboxModel).where(OutboxModel.id.in_(ids)))
                await cleanup.commit()
