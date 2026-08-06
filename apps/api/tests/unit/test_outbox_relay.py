"""`OutboxRelay` — the four outbox behaviours A64-013.7 names, plus the two
that make at-least-once safe.

    enqueue    `TestEnqueue` — the publisher stages a row and does not commit
    dequeue    `TestClaiming` — due entries only, oldest first, bounded
    processed  `TestMarkingProcessed` — the ledger, and what it de-duplicates
    retry      `TestRetry` — backoff, the ceiling, and what stops being tried

Runs the **real** relay and the **real** publisher over in-memory storage.
Substituting the relay would leave the sequencing untested, which is where
every interesting bug in a delivery pipeline is; substituting PostgreSQL
loses `SKIP LOCKED`, which is why that one property is asserted in
`tests/contract/test_outbox_repository.py` instead of pretended to here.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest

from app.platform.events import DomainEvent
from app.platform.outbox import OutboxEntry, OutboxEventPublisher
from app.platform.outbox.relay import DeliveryFailure, OutboxRelay
from tests.fakes.outbox import (
    InMemoryOutbox,
    InMemoryProcessedEvents,
    NullUnitOfWork,
    SingleUseUnitOfWork,
)
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
SUBJECT = UUID("019fbc10-1111-7000-8000-000000000001")


@dataclass(frozen=True)
class _Thing(DomainEvent):
    """A minimal event, so the relay's tests do not depend on a real one.

    A social event would work and would couple every assertion below to
    `friends`' payload shape — which is the coupling the outbox exists to
    avoid: the relay does not know what an event means.
    """

    event_type: ClassVar[str] = "test.thing_happened"
    aggregate_type: ClassVar[str] = "thing"

    thing_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.thing_id

    def payload(self) -> dict[str, Any]:
        return {"thing_id": str(self.thing_id)}


class _Handler:
    """A consumer that records what it was given and fails on request."""

    def __init__(
        self,
        *,
        consumer: str = "test_consumer",
        subscribes_to: str = _Thing.event_type,
        fail_ids: set[UUID] | None = None,
        raises: bool = False,
    ) -> None:
        self._consumer = consumer
        self._subscribes_to = subscribes_to
        self._fail_ids = fail_ids or set()
        self._raises = raises
        self.batches: list[list[UUID]] = []

    @property
    def consumer(self) -> str:
        return self._consumer

    def handles(self, event_type: str) -> bool:
        return event_type == self._subscribes_to

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[DeliveryFailure]:
        self.batches.append([entry.id for entry in entries])
        if self._raises:
            raise RuntimeError("consumer exploded")
        return [
            DeliveryFailure(entry.id, "Refused") for entry in entries if entry.id in self._fail_ids
        ]


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def outbox() -> InMemoryOutbox:
    return InMemoryOutbox()


@pytest.fixture
def processed() -> InMemoryProcessedEvents:
    return InMemoryProcessedEvents()


def _relay(
    outbox: InMemoryOutbox,
    processed: InMemoryProcessedEvents,
    clock: MovableClock,
    *handlers: _Handler,
    batch_size: int = 50,
    max_attempts: int = 5,
    unit_of_work: object | None = None,
) -> OutboxRelay:
    return OutboxRelay(
        outbox=cast(Any, outbox),
        processed=cast(Any, processed),
        handlers=cast(Any, list(handlers)),
        unit_of_work=cast(Any, unit_of_work or NullUnitOfWork()),
        clock=clock,
        worker_id="test-worker",
        batch_size=batch_size,
        max_attempts=max_attempts,
        retry_base_seconds=5,
        retry_max_seconds=300,
    )


async def _enqueue(outbox: InMemoryOutbox, *, at: datetime | None = None) -> OutboxEntry:
    publisher = OutboxEventPublisher(cast(Any, outbox))
    return await publisher.publish(_Thing(occurred_at=at or NOW, thing_id=SUBJECT))


class TestEnqueue:
    async def test_publishing_stages_a_durable_entry(self, outbox: InMemoryOutbox) -> None:
        """The producer half of AD-16: the event becomes a row."""
        entry = await _enqueue(outbox)

        stored = await outbox.get(entry.id)
        assert stored is not None
        assert stored.event_type == "test.thing_happened"
        assert stored.aggregate_id == SUBJECT

    async def test_the_payload_survives_as_json_primitives(self, outbox: InMemoryOutbox) -> None:
        """`jsonb` holds strings, not `UUID`s. A payload that only encodes
        because a custom default was installed stops encoding the day
        something else writes the column."""
        entry = await _enqueue(outbox)

        assert entry.payload == {"thing_id": str(SUBJECT)}

    async def test_a_published_entry_starts_unpublished_and_unattempted(
        self, outbox: InMemoryOutbox
    ) -> None:
        entry = await _enqueue(outbox)

        assert entry.published_at is None
        assert entry.attempt_count == 0
        # Null rather than `occurred_at`, so "never attempted" and "backed
        # off to exactly now" stay distinguishable in the table.
        assert entry.next_attempt_at is None

    async def test_two_events_get_two_identities(self, outbox: InMemoryOutbox) -> None:
        """The id is generated in the application (DB-07), which is what lets
        a producer log the event id in the request that emitted it."""
        first, second = await _enqueue(outbox), await _enqueue(outbox)

        assert first.id != second.id


class TestClaiming:
    async def test_a_claimed_entry_reaches_its_subscriber(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        entry = await _enqueue(outbox)
        handler = _Handler()

        tick = await _relay(outbox, processed, clock, handler).run_once()

        assert handler.batches == [[entry.id]]
        assert tick.claimed == 1
        assert tick.published == 1

    async def test_an_empty_outbox_is_an_idle_tick(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """The common case on a quiet platform, and it must cost nothing —
        no handler call, no publish."""
        handler = _Handler()

        tick = await _relay(outbox, processed, clock, handler).run_once()

        assert tick.is_idle
        assert handler.batches == []

    async def test_entries_arrive_in_causation_order(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """database.md §12.5: publication order follows `occurred_at`, not
        insertion order — so an event recorded out of sequence still reaches
        a consumer in the order the facts happened."""
        later = await _enqueue(outbox, at=NOW + timedelta(seconds=10))
        earlier = await _enqueue(outbox, at=NOW)
        handler = _Handler()

        await _relay(outbox, processed, clock, handler).run_once()

        assert handler.batches == [[earlier.id, later.id]]

    async def test_a_tick_claims_no_more_than_the_batch_size(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """Bounded, per CLAUDE.md §10.5. An unbounded claim is one tick
        holding a handler's I/O over every row in the backlog."""
        for offset in range(5):
            await _enqueue(outbox, at=NOW + timedelta(seconds=offset))
        handler = _Handler()

        tick = await _relay(outbox, processed, clock, handler, batch_size=2).run_once()

        assert tick.claimed == 2

    async def test_the_batch_reaches_the_handler_as_one_call(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """Batch-first delivery: three events cost one `handle`, not three.
        A consumer that reads profiles or relationships can therefore do it
        once for the tick."""
        for offset in range(3):
            await _enqueue(outbox, at=NOW + timedelta(seconds=offset))
        handler = _Handler()

        await _relay(outbox, processed, clock, handler).run_once()

        assert len(handler.batches) == 1
        assert len(handler.batches[0]) == 3

    async def test_a_published_entry_is_never_claimed_again(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        await _enqueue(outbox)
        relay = _relay(outbox, processed, clock, _Handler())

        await relay.run_once()
        second = await relay.run_once()

        assert second.is_idle

    async def test_an_event_nobody_subscribes_to_is_published_not_stranded(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """Left unpublished it would grow the backlog metric forever — the
        one number that says whether the relay is healthy. The row is
        retained either way (AD-17), so a subscriber added later replays from
        the table."""
        entry = await _enqueue(outbox)
        uninterested = _Handler(subscribes_to="something.else")

        tick = await _relay(outbox, processed, clock, uninterested).run_once()

        stored = await outbox.get(entry.id)
        assert stored is not None and stored.published_at is not None
        assert tick.skipped == 1
        assert uninterested.batches == []

    async def test_a_skipped_entry_names_its_type_in_the_tick_log(
        self,
        outbox: InMemoryOutbox,
        processed: InMemoryProcessedEvents,
        clock: MovableClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A64-021.2H — the regression this phase exists for.

        Publishing an entry nobody wants is correct; doing it **invisibly**
        is what turned a stale node into silent, unrecoverable loss. A node
        whose build predates an event type claims it, discards it, leaves no
        ledger row, and — before this — said nothing at all. A person
        noticing a missing notification was the only detector.

        The count alone is not the signal: most of this platform's event
        types have no subscriber, so a non-zero `skipped` is ordinary. What
        identifies a build skew is **which** types were dropped, compared
        against what this node subscribes to.

        Asserted on the log record's fields rather than its message, because
        the fields are what an aggregator queries.
        """
        await _enqueue(outbox)
        uninterested = _Handler(subscribes_to="something.else")

        with caplog.at_level(logging.INFO, logger="app.platform.outbox.relay"):
            await _relay(outbox, processed, clock, uninterested).run_once()

        tick = next(r for r in caplog.records if r.message == "outbox_tick_completed")
        assert tick.skipped == 1
        assert tick.skipped_event_types == [_Thing.event_type]
        # The two are distinct: this entry was published, and it was not
        # delivered to anybody. A reader of one number could not tell.
        assert tick.published == 1


class TestMarkingProcessed:
    async def test_a_delivered_event_is_recorded_against_its_consumer(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        entry = await _enqueue(outbox)

        await _relay(outbox, processed, clock, _Handler()).run_once()

        assert ("test_consumer", entry.id) in processed.records

    async def test_a_redelivered_event_is_not_handed_over_twice(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """At-least-once delivery is a certainty, not a risk (AD-16). The
        ledger is what turns it into at-most-once *effect* — asserted by
        replaying an entry the relay has already delivered."""
        entry = await _enqueue(outbox)
        handler = _Handler()
        await _relay(outbox, processed, clock, handler).run_once()

        # The crash-and-redeliver shape: the row is pending again, but the
        # ledger remembers.
        outbox.entries[entry.id] = _unpublish(outbox.entries[entry.id])
        await _relay(outbox, processed, clock, handler).run_once()

        assert handler.batches == [[entry.id]]

    async def test_two_consumers_have_independent_ledgers(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """The key is `(consumer, event_id)`, so adding a second subscriber
        does not silently inherit the first's history — nor skip its own."""
        await _enqueue(outbox)
        first, second = _Handler(consumer="one"), _Handler(consumer="two")

        await _relay(outbox, processed, clock, first, second).run_once()

        assert len(first.batches) == 1
        assert len(second.batches) == 1

    async def test_a_failed_entry_is_not_recorded_as_processed(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """Otherwise the retry would be filtered out by the ledger and the
        event would be permanently undelivered while looking handled."""
        entry = await _enqueue(outbox)
        handler = _Handler(fail_ids={entry.id})

        await _relay(outbox, processed, clock, handler).run_once()

        assert ("test_consumer", entry.id) not in processed.records


class TestRetry:
    async def test_a_failure_leaves_the_entry_unpublished(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        entry = await _enqueue(outbox)

        tick = await _relay(outbox, processed, clock, _Handler(fail_ids={entry.id})).run_once()

        stored = await outbox.get(entry.id)
        assert stored is not None and stored.published_at is None
        assert tick.failed == 1

    async def test_the_reason_is_recorded_on_the_row(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """`last_error` is what an operator queries when asked why an event
        never arrived — the log line alone has a retention policy nobody
        chose for this purpose."""
        entry = await _enqueue(outbox)

        await _relay(outbox, processed, clock, _Handler(fail_ids={entry.id})).run_once()

        stored = await outbox.get(entry.id)
        assert stored is not None and stored.last_error == "Refused"

    async def test_a_failed_entry_backs_off_before_the_next_attempt(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """Without a "not before" instant, retry is a tight loop against
        whatever is failing."""
        entry = await _enqueue(outbox)
        relay = _relay(outbox, processed, clock, _Handler(fail_ids={entry.id}))

        await relay.run_once()
        immediate = await relay.run_once()

        assert immediate.is_idle

    async def test_the_entry_is_retried_once_the_backoff_elapses(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        entry = await _enqueue(outbox)
        handler = _Handler(fail_ids={entry.id})
        relay = _relay(outbox, processed, clock, handler)

        await relay.run_once()
        clock.advance(6)
        await relay.run_once()

        assert handler.batches == [[entry.id], [entry.id]]

    async def test_the_backoff_grows_with_each_attempt(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """`base * 2 ** (attempt - 1)`: five seconds, then ten. The first
        retry waits exactly `retry_base_seconds` rather than twice it — an
        off-by-one that is invisible until somebody watches a stuck queue."""
        entry = await _enqueue(outbox)
        relay = _relay(outbox, processed, clock, _Handler(fail_ids={entry.id}))

        await relay.run_once()
        first = await _next_attempt_at(outbox, entry.id)
        clock.advance(6)
        await relay.run_once()
        second = await _next_attempt_at(outbox, entry.id)

        assert first == NOW + timedelta(seconds=5)
        assert second == NOW + timedelta(seconds=6 + 10)

    async def test_an_entry_stops_being_claimed_at_the_ceiling(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """Exhausted, and deliberately still *unpublished* — see `OutboxEntry`
        on why a permanently failing event must keep growing the backlog
        metric rather than moving to a dead-letter table nobody watches."""
        entry = await _enqueue(outbox)
        handler = _Handler(fail_ids={entry.id})
        relay = _relay(outbox, processed, clock, handler, max_attempts=2)

        for _ in range(4):
            await relay.run_once()
            clock.advance(600)

        stored = await outbox.get(entry.id)
        assert stored is not None
        assert stored.attempt_count == 2
        assert stored.published_at is None
        assert len(handler.batches) == 2

    async def test_a_handler_that_raises_fails_only_its_own_batch(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """An exception is the unclassified case — the consumer did not say
        which part of the batch survived — so all of it is retried. The relay
        itself still returns rather than propagating, because a relay that
        raised would stop the loop that calls it."""
        entry = await _enqueue(outbox)

        tick = await _relay(outbox, processed, clock, _Handler(raises=True)).run_once()

        assert tick.failed == 1
        stored = await outbox.get(entry.id)
        assert stored is not None and stored.last_error == "RuntimeError"

    async def test_one_poison_event_does_not_hold_back_the_batch(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """The whole reason `handle` returns failures instead of raising: a
        batch that failed as a unit would mean one bad event retries — and
        re-fails — with every event claimed beside it."""
        good = await _enqueue(outbox, at=NOW)
        bad = await _enqueue(outbox, at=NOW + timedelta(seconds=1))

        tick = await _relay(outbox, processed, clock, _Handler(fail_ids={bad.id})).run_once()

        assert tick.published == 1
        assert tick.failed == 1
        assert (await outbox.get(good.id)) is not None
        stored_good = await outbox.get(good.id)
        assert stored_good is not None and stored_good.published_at is not None


async def _next_attempt_at(outbox: InMemoryOutbox, entry_id: UUID) -> datetime | None:
    entry = await outbox.get(entry_id)
    assert entry is not None
    return entry.next_attempt_at


def _unpublish(entry: OutboxEntry) -> OutboxEntry:
    """The row as a crash between delivery and the publication commit would
    leave it: handled, but still pending."""
    from dataclasses import replace

    return replace(entry, published_at=None, attempt_count=0, next_attempt_at=None)


class TestTheRelaySessionIsNotShared:
    @pytest.mark.asyncio
    async def test_concurrent_consumers_never_enter_the_relay_session_together(self) -> None:
        """The regression for a defect that reached production —
        A64-020.5F.

        `_dispatch` runs the consumers under `asyncio.gather`, and each
        delivery brackets its handler with two blocks on the relay's **own**
        session: the idempotency filter before, the ledger write after. Those
        were unguarded, so two consumers interleaved statements on one
        `AsyncSession` — which asyncpg refuses and SQLAlchemy reports as
        `IllegalStateChangeError` from a rollback that could not run.

        It reached production because every test here drove the relay with
        `NullUnitOfWork`, which models a transaction boundary over nothing
        and cannot express "a session is one connection".
        `SingleUseUnitOfWork` models exactly that and nothing else.

        The handlers below **await** inside `handle`, which is what makes
        the interleaving happen: without a suspension point the event loop
        never switches and the bug hides.
        """
        outbox = InMemoryOutbox()
        processed = InMemoryProcessedEvents()
        clock = MovableClock(NOW)
        await _enqueue(outbox)

        class _Slow(_Handler):
            async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[DeliveryFailure]:
                # Two suspensions, so this consumer is guaranteed to be
                # mid-flight while the others run their own bracketing
                # blocks.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                return await super().handle(entries)

        session = SingleUseUnitOfWork()
        relay = _relay(
            outbox,
            processed,
            clock,
            _Slow(consumer="alpha"),
            _Slow(consumer="beta"),
            _Slow(consumer="gamma"),
            unit_of_work=session,
        )

        tick = await relay.run_once()

        # Every consumer delivered, and none of them found the session
        # occupied — which is the assertion, because the fake raises rather
        # than recording if they had.
        assert tick.published == 1
        assert tick.failed == 0
        for consumer in ("alpha", "beta", "gamma"):
            assert any(record[0] == consumer for record in processed.records)
