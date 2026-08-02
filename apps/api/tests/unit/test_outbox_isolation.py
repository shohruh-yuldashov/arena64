"""Per-consumer isolation on the relay — A64-015.6 §5.

A64-015.5 left the relay with three consumers on one loop and named the risk
in its own recommendations: "`OutboxSettings.batch_size` is shared; a slow
sink would delay the acceptance-failure policy." The audit found the problem
was worse than that — handlers ran **sequentially** with **no timeout at
all**, so a consumer that hung stopped the relay for the whole process
indefinitely.

This file is the evidence for both halves of the fix, and it measures rather
than asserts intent: the slow-consumer tests use real `asyncio.sleep` on a
scale small enough to be fast and large enough to separate concurrent from
sequential by a wide margin.

The real relay, the real `ConsumerPolicies` and real `asyncio` scheduling run
here. What is substituted is storage and the clock.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest

from app.platform.events import DomainEvent
from app.platform.outbox import (
    DEFAULT_CONSUMER_TIMEOUT_SECONDS,
    ConsumerPolicies,
    ConsumerPolicy,
    OutboxEntry,
    OutboxEventPublisher,
)
from app.platform.outbox.relay import DeliveryFailure, OutboxRelay
from tests.fakes.outbox import InMemoryOutbox, InMemoryProcessedEvents, NullUnitOfWork
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
SUBJECT = UUID("019fbc10-2222-7000-8000-000000000002")

#: How long the "slow" consumer takes. Long enough that a sequential relay
#: would take a multiple of it and a concurrent one would not, short enough
#: that the suite does not notice.
SLOW = 0.05


@dataclass(frozen=True)
class _Thing(DomainEvent):
    """A minimal event — the relay does not know what an event means, and
    neither should its tests."""

    event_type: ClassVar[str] = "test.thing_happened"
    aggregate_type: ClassVar[str] = "thing"

    thing_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.thing_id

    def payload(self) -> dict[str, Any]:
        return {"thing_id": str(self.thing_id)}


class _Consumer:
    """A consumer that takes a configurable amount of time.

    `delay=0` still yields to the loop, so a "fast" consumer genuinely
    interleaves rather than running to completion inside one step — without
    that, concurrency would be untestable because nothing would ever suspend.
    """

    def __init__(self, name: str, *, delay: float = 0.0, hangs: bool = False) -> None:
        self._name = name
        self._delay = delay
        self._hangs = hangs
        self.finished_at: float | None = None
        self.batches: list[list[UUID]] = []

    @property
    def consumer(self) -> str:
        return self._name

    def handles(self, event_type: str) -> bool:
        return event_type == _Thing.event_type

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[DeliveryFailure]:
        self.batches.append([entry.id for entry in entries])
        if self._hangs:
            # Not "slow" — stuck. The failure mode the relay had no bound
            # for, modelled as an await that never resolves on its own.
            await asyncio.Event().wait()
        await asyncio.sleep(self._delay)
        self.finished_at = asyncio.get_running_loop().time()
        return []


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
    *consumers: _Consumer,
    policies: ConsumerPolicies | None = None,
) -> OutboxRelay:
    return OutboxRelay(
        outbox=cast(Any, outbox),
        processed=cast(Any, processed),
        handlers=cast(Any, list(consumers)),
        unit_of_work=cast(Any, NullUnitOfWork()),
        clock=clock,
        worker_id="test-worker",
        batch_size=50,
        max_attempts=5,
        retry_base_seconds=5,
        retry_max_seconds=300,
        policies=policies,
    )


async def _enqueue(outbox: InMemoryOutbox) -> OutboxEntry:
    return await OutboxEventPublisher(cast(Any, outbox)).publish(
        _Thing(occurred_at=NOW, thing_id=SUBJECT)
    )


class TestAPolicyIsAValidatedValue:
    def test_a_consumer_with_no_policy_still_has_a_bound(self) -> None:
        """The direction that matters. The failure this module exists to
        prevent is an *unbounded* wait, so forgetting to register a policy
        must not reintroduce it."""
        assert ConsumerPolicies.of().timeout_for("anyone") == DEFAULT_CONSUMER_TIMEOUT_SECONDS

    def test_a_registered_consumer_gets_its_own(self) -> None:
        policies = ConsumerPolicies.of([ConsumerPolicy(consumer="realtime", timeout_seconds=10.0)])

        assert policies.timeout_for("realtime") == 10.0

    def test_a_zero_timeout_is_refused_at_construction(self) -> None:
        """It would fail every batch instantly, and discovering that from an
        empty timeline at 3am is worse than discovering it at startup."""
        with pytest.raises(ValueError, match="positive"):
            ConsumerPolicy(consumer="realtime", timeout_seconds=0.0)

    def test_a_negative_timeout_is_refused(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            ConsumerPolicy(consumer="realtime", timeout_seconds=-1.0)

    def test_two_policies_for_one_consumer_are_refused(self) -> None:
        """Silently applying one of them is the kind of thing only noticed
        when the other was the one that mattered."""
        with pytest.raises(ValueError, match="two policies"):
            ConsumerPolicies.of(
                [
                    ConsumerPolicy(consumer="realtime", timeout_seconds=10.0),
                    ConsumerPolicy(consumer="realtime", timeout_seconds=20.0),
                ]
            )


class TestASlowConsumerDoesNotDelayTheOthers:
    """§5: "a slow consumer must not delay unrelated consumers"."""

    @pytest.mark.asyncio
    async def test_a_tick_costs_the_slowest_consumer_rather_than_the_sum(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """Four slow consumers sequentially would be `4 * SLOW`. The margin
        is deliberately wide so the assertion is not a timing race."""
        consumers = [_Consumer(f"slow_{index}", delay=SLOW) for index in range(4)]
        relay = _relay(outbox, processed, clock, *consumers)
        await _enqueue(outbox)

        started = asyncio.get_running_loop().time()
        await relay.run_once()
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < SLOW * 3

    @pytest.mark.asyncio
    async def test_a_fast_consumer_finishes_before_a_slow_one_started_first(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """Registration order used to decide who waited for whom, which made
        latency a property of a list literal at the composition root."""
        slow, fast = _Consumer("slow", delay=SLOW), _Consumer("fast")
        relay = _relay(outbox, processed, clock, slow, fast)
        await _enqueue(outbox)

        await relay.run_once()

        assert fast.finished_at is not None
        assert slow.finished_at is not None
        assert fast.finished_at < slow.finished_at

    @pytest.mark.asyncio
    async def test_every_consumer_still_receives_the_batch(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """Concurrency must not cost delivery. §5 requires durability to be
        unchanged, and this is the observable half of it."""
        slow, fast = _Consumer("slow", delay=SLOW), _Consumer("fast")
        relay = _relay(outbox, processed, clock, slow, fast)
        entry = await _enqueue(outbox)

        await relay.run_once()

        assert slow.batches == [[entry.id]]
        assert fast.batches == [[entry.id]]


class TestAStuckConsumerFailsOnlyItsOwnSlice:
    """The bound that did not exist before A64-015.6."""

    @pytest.mark.asyncio
    async def test_the_relay_returns_rather_than_waiting_forever(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """Before this, a consumer that hung stopped the relay for the whole
        process. The test would not terminate without the timeout."""
        relay = _relay(
            outbox,
            processed,
            clock,
            _Consumer("stuck", hangs=True),
            policies=ConsumerPolicies.of([ConsumerPolicy(consumer="stuck", timeout_seconds=SLOW)]),
        )
        await _enqueue(outbox)

        tick = await asyncio.wait_for(relay.run_once(), timeout=5)

        assert tick.failed == 1

    @pytest.mark.asyncio
    async def test_a_healthy_consumer_beside_it_still_completes(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        healthy = _Consumer("healthy")
        relay = _relay(
            outbox,
            processed,
            clock,
            _Consumer("stuck", hangs=True),
            healthy,
            policies=ConsumerPolicies.of([ConsumerPolicy(consumer="stuck", timeout_seconds=SLOW)]),
        )
        entry = await _enqueue(outbox)

        await asyncio.wait_for(relay.run_once(), timeout=5)

        assert healthy.batches == [[entry.id]]

    @pytest.mark.asyncio
    async def test_the_healthy_consumers_work_is_recorded(
        self,
        outbox: InMemoryOutbox,
        processed: InMemoryProcessedEvents,
        clock: MovableClock,
    ) -> None:
        """The ledger is per `(entry, consumer)`, so the consumer that
        succeeded is not asked again when the entry is retried for the one
        that timed out."""
        relay = _relay(
            outbox,
            processed,
            clock,
            _Consumer("stuck", hangs=True),
            _Consumer("healthy"),
            policies=ConsumerPolicies.of([ConsumerPolicy(consumer="stuck", timeout_seconds=SLOW)]),
        )
        entry = await _enqueue(outbox)

        await asyncio.wait_for(relay.run_once(), timeout=5)

        assert ("healthy", entry.id) in processed.records

    @pytest.mark.asyncio
    async def test_the_timed_out_consumer_did_not_record_the_entry(
        self,
        outbox: InMemoryOutbox,
        processed: InMemoryProcessedEvents,
        clock: MovableClock,
    ) -> None:
        """So the retry actually reaches it. A timeout that marked the entry
        processed would be a silent drop."""
        relay = _relay(
            outbox,
            processed,
            clock,
            _Consumer("stuck", hangs=True),
            policies=ConsumerPolicies.of([ConsumerPolicy(consumer="stuck", timeout_seconds=SLOW)]),
        )
        entry = await _enqueue(outbox)

        await asyncio.wait_for(relay.run_once(), timeout=5)

        assert ("stuck", entry.id) not in processed.records

    @pytest.mark.asyncio
    async def test_the_entry_stays_claimable(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """A timed-out slice is retried on the row, which is what makes the
        isolation a *delay* rather than a loss."""
        relay = _relay(
            outbox,
            processed,
            clock,
            _Consumer("stuck", hangs=True),
            policies=ConsumerPolicies.of([ConsumerPolicy(consumer="stuck", timeout_seconds=SLOW)]),
        )
        entry = await _enqueue(outbox)

        await asyncio.wait_for(relay.run_once(), timeout=5)

        stored = await outbox.get(entry.id)
        assert stored is not None
        assert stored.published_at is None
        assert stored.next_attempt_at is not None

    @pytest.mark.asyncio
    async def test_the_failure_names_the_timeout_rather_than_the_consumer(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """`last_error` is an operator's first clue, and "delivery_timeout"
        distinguishes a stuck consumer from one that raised."""
        relay = _relay(
            outbox,
            processed,
            clock,
            _Consumer("stuck", hangs=True),
            policies=ConsumerPolicies.of([ConsumerPolicy(consumer="stuck", timeout_seconds=SLOW)]),
        )
        entry = await _enqueue(outbox)

        await asyncio.wait_for(relay.run_once(), timeout=5)

        stored = await outbox.get(entry.id)
        assert stored is not None
        assert stored.last_error == "delivery_timeout"


class TestBudgetsArePerConsumer:
    """§5: "different consumers can have isolated retry and failure
    policies"."""

    @pytest.mark.asyncio
    async def test_a_consumer_inside_its_budget_is_unaffected_by_a_tighter_one_beside_it(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """The tight budget belongs to the consumer it names, not to the
        tick — which is the difference between isolation and a global
        deadline."""
        patient = _Consumer("patient", delay=SLOW)
        relay = _relay(
            outbox,
            processed,
            clock,
            patient,
            _Consumer("impatient", hangs=True),
            policies=ConsumerPolicies.of(
                [
                    ConsumerPolicy(consumer="patient", timeout_seconds=5.0),
                    ConsumerPolicy(consumer="impatient", timeout_seconds=SLOW / 5),
                ]
            ),
        )
        await _enqueue(outbox)

        await asyncio.wait_for(relay.run_once(), timeout=5)

        assert patient.finished_at is not None

    @pytest.mark.asyncio
    async def test_a_consumer_that_raises_does_not_time_out_its_neighbour(
        self, outbox: InMemoryOutbox, processed: InMemoryProcessedEvents, clock: MovableClock
    ) -> None:
        """An exception is already converted to per-entry failures; this
        asserts the concurrent dispatch did not turn one consumer's crash
        into everyone's."""

        class _Exploding(_Consumer):
            async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[DeliveryFailure]:
                raise RuntimeError("consumer exploded")

        healthy = _Consumer("healthy")
        relay = _relay(outbox, processed, clock, _Exploding("exploding"), healthy)
        entry = await _enqueue(outbox)

        await relay.run_once()

        assert healthy.batches == [[entry.id]]
        assert ("healthy", entry.id) in processed.records
