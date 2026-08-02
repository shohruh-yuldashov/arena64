"""The pairing reconciliation timeline — A64-015.6 §4.

A64-015.5 published `matchmaking.pairing_reconciled` and recorded the gap:
"It is written on every recovery and read by nobody." This file is the
evidence that an operator holding a ticket id can now answer "why did this
player's ticket go back into the queue at 03:12".

`ReconciliationTimelineProjector` and `ReconciliationEntry` run **for real**
over in-memory storage. What is substituted is the database and the clock.

The projection's idempotency is asserted here through a fake that models its
unique index, and against the index itself in
`tests/contract/test_matchmaking_audit.py` — the split every fake on this
platform makes, because two concurrent relays resolving in one statement is a
property of PostgreSQL rather than of this code.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.matchmaking.application.services.reconciliation_timeline_service import (
    CONSUMER_NAME,
    ReconciliationTimelineProjector,
)
from app.modules.matchmaking.domain.events import PairingReconciled, ReconciliationAction
from app.platform.outbox import OutboxEntry
from tests.fakes.audit import InMemoryReconciliationTimelineRepository
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
RESERVED_UNTIL = NOW - timedelta(seconds=45)

#: The relay is behind by this much in every test that cares, so
#: `recorded_at` and `occurred_at` are never accidentally equal — the gap
#: between them is relay lag, which is the thing an operator reads.
LAG = timedelta(seconds=12)


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW + LAG)


@pytest.fixture
def timeline() -> InMemoryReconciliationTimelineRepository:
    return InMemoryReconciliationTimelineRepository()


@pytest.fixture
def unit_of_work() -> NullUnitOfWork:
    return NullUnitOfWork()


@pytest.fixture
def projector(
    timeline: InMemoryReconciliationTimelineRepository,
    unit_of_work: NullUnitOfWork,
    clock: MovableClock,
) -> ReconciliationTimelineProjector:
    return ReconciliationTimelineProjector(
        timeline=timeline, unit_of_work=unit_of_work, clock=clock
    )


def _reconciled(
    action: ReconciliationAction = ReconciliationAction.REQUEUED,
    *,
    ticket_id: object = None,
    match_id: object = None,
    occurred_at: datetime = NOW,
) -> OutboxEntry:
    """One `matchmaking.pairing_reconciled`, as the relay hands it over."""
    return OutboxEntry.of(
        PairingReconciled(
            occurred_at=occurred_at,
            ticket_id=ticket_id if ticket_id is not None else generate_uuid7(),  # type: ignore[arg-type]
            player_id=generate_uuid7(),
            action=action,
            match_id=match_id,  # type: ignore[arg-type]
            reserved_until=RESERVED_UNTIL,
        )
    )


class TestTheConsumerSubscribesToOneEvent:
    def test_it_handles_the_reconciliation_event(
        self, projector: ReconciliationTimelineProjector
    ) -> None:
        assert projector.handles(PairingReconciled.event_type) is True

    def test_it_handles_nothing_else(self, projector: ReconciliationTimelineProjector) -> None:
        """A projector that accepted every event would be a second consumer
        of the acceptance-failure stream, writing rows nothing asked for."""
        assert projector.handles("game.match_declined") is False

    def test_it_has_its_own_ledger_partition(
        self, projector: ReconciliationTimelineProjector
    ) -> None:
        """`platform.processed_event` is keyed on `(entry, consumer)`, so a
        name shared with another consumer would make each suppress the
        other's delivery."""
        assert projector.consumer == CONSUMER_NAME


class TestAnEventBecomesATimelineEntry:
    """§4: "every reconciliation action produces a timeline entry"."""

    @pytest.mark.asyncio
    async def test_one_event_writes_one_entry(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        assert await projector.handle([_reconciled()]) == []
        assert len(timeline.entries) == 1

    @pytest.mark.asyncio
    async def test_the_entry_is_keyed_on_the_ticket(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """The identifier a support conversation starts from, and the reason
        this is a relation rather than a log query."""
        ticket_id = generate_uuid7()

        await projector.handle([_reconciled(ticket_id=ticket_id)])

        assert timeline.entries[0].ticket_id == ticket_id

    @pytest.mark.asyncio
    async def test_the_entry_carries_the_action(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        await projector.handle([_reconciled(ReconciliationAction.SETTLED, match_id=uuid4())])

        assert timeline.entries[0].action is ReconciliationAction.SETTLED

    @pytest.mark.asyncio
    async def test_a_settled_ticket_names_the_match_it_turned_out_to_have(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """ "Was this player's game already created when we found their ticket
        stranded" is the whole content of a settled entry."""
        match_id = uuid4()

        await projector.handle([_reconciled(ReconciliationAction.SETTLED, match_id=match_id)])

        assert timeline.entries[0].match_id == match_id

    @pytest.mark.asyncio
    async def test_a_requeued_ticket_names_no_match(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        await projector.handle([_reconciled(ReconciliationAction.REQUEUED)])

        assert timeline.entries[0].match_id is None

    @pytest.mark.asyncio
    async def test_every_action_the_reconciler_can_take_projects(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """§4 says *every* action, so the test enumerates the enum rather
        than a sample — a member added later fails here until it projects."""
        for action in ReconciliationAction:
            match_id = uuid4() if action is ReconciliationAction.SETTLED else None
            await projector.handle([_reconciled(action, match_id=match_id)])

        assert {entry.action for entry in timeline.entries} == set(ReconciliationAction)

    @pytest.mark.asyncio
    async def test_a_batch_projects_every_entry(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        await projector.handle([_reconciled() for _ in range(5)])

        assert len(timeline.entries) == 5


class TestTheEntryIsOrderedByWhatHappened:
    @pytest.mark.asyncio
    async def test_occurred_at_comes_from_the_event(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """Not from the clock. A relay that catches up after an outage would
        otherwise write a timeline ordered by when it recovered."""
        await projector.handle([_reconciled(occurred_at=NOW)])

        assert timeline.entries[0].occurred_at == NOW

    @pytest.mark.asyncio
    async def test_recorded_at_comes_from_the_clock(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        assert_at = NOW + LAG

        await projector.handle([_reconciled(occurred_at=NOW)])

        assert timeline.entries[0].recorded_at == assert_at

    @pytest.mark.asyncio
    async def test_the_two_are_kept_apart_so_lag_is_readable(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """The gap is relay lag, which is exactly what an operator asking
        "why was this late" wants and is not derivable from either alone."""
        await projector.handle([_reconciled(occurred_at=NOW)])
        entry = timeline.entries[0]

        assert entry.recorded_at - entry.occurred_at == LAG


class TestARedeliveredEventProjectsOnce:
    """§4: "duplicate reconciliation events must not corrupt the timeline"."""

    @pytest.mark.asyncio
    async def test_the_same_entry_twice_writes_one_row(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """AD-16 delivers at least once and two relays can deliver
        concurrently, so this is the ordinary path."""
        entry = _reconciled()

        await projector.handle([entry])
        await projector.handle([entry])

        assert len(timeline.entries) == 1

    @pytest.mark.asyncio
    async def test_a_redelivery_reports_no_failure(
        self, projector: ReconciliationTimelineProjector
    ) -> None:
        """A duplicate is a **no-op**, not an error: reporting it would spend
        the entry's attempt budget on work that is already done."""
        entry = _reconciled()
        await projector.handle([entry])

        assert await projector.handle([entry]) == []

    @pytest.mark.asyncio
    async def test_the_surviving_row_is_the_first_projections(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
        clock: MovableClock,
    ) -> None:
        """`recorded_at` must not move on a redelivery — a timeline whose lag
        changed on retry would misreport when the platform found out."""
        entry = _reconciled()
        await projector.handle([entry])
        first_recorded_at = timeline.entries[0].recorded_at

        clock.advance(3600)
        await projector.handle([entry])

        assert timeline.entries[0].recorded_at == first_recorded_at

    @pytest.mark.asyncio
    async def test_a_batch_containing_a_duplicate_still_projects_the_rest(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        seen = _reconciled()
        await projector.handle([seen])

        await projector.handle([seen, _reconciled(), _reconciled()])

        assert len(timeline.entries) == 3


class TestAFailureIsSeparableFromAResolution:
    @pytest.mark.asyncio
    async def test_a_failed_reconciliation_is_marked(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """The one action that is not a resolution: the others say what became
        of a ticket, and this one says the tick could not say."""
        await projector.handle([_reconciled(ReconciliationAction.FAILED)])

        assert timeline.entries[0].is_failure is True

    @pytest.mark.asyncio
    async def test_a_resolution_is_not(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        await projector.handle([_reconciled(ReconciliationAction.REQUEUED)])

        assert timeline.entries[0].is_failure is False


class TestABrokenPayloadFailsOnlyItsOwnEntry:
    @pytest.mark.asyncio
    async def test_a_payload_missing_the_ticket_is_rejected(
        self, projector: ReconciliationTimelineProjector
    ) -> None:
        """A producer bug must not stop the timeline recording everything
        else, and must not be retried forever as if it were transient."""
        broken = _reconciled()
        object.__setattr__(broken, "payload", {"player_id": str(uuid4())})

        failures = await projector.handle([broken])

        assert [failure.entry_id for failure in failures] == [broken.id]

    @pytest.mark.asyncio
    async def test_the_rest_of_the_batch_still_projects(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        broken = _reconciled()
        object.__setattr__(broken, "payload", {"player_id": str(uuid4())})

        await projector.handle([broken, _reconciled(), _reconciled()])

        assert len(timeline.entries) == 2


class TestAStorageFailureIsRetryable:
    @pytest.mark.asyncio
    async def test_every_entry_in_the_batch_is_reported(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """The relation being unreachable is transient, so the whole batch
        goes back for its own backoff rather than being dropped."""
        timeline.fails = True
        entries = [_reconciled() for _ in range(3)]

        failures = await projector.handle(entries)

        assert {failure.entry_id for failure in failures} == {entry.id for entry in entries}

    @pytest.mark.asyncio
    async def test_it_does_not_raise_into_the_relay(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """A consumer that raised would fail the tick for every other
        consumer sharing it — the isolation A64-015.6 §5 is about."""
        timeline.fails = True

        assert await projector.handle([_reconciled()]) != []


class TestTheOperatorQueries:
    """§4: "the timeline is queryable by ticket and by pairing identifier"."""

    @pytest.mark.asyncio
    async def test_a_tickets_history_is_most_recent_first(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        ticket_id = generate_uuid7()
        for minutes in (0, 5, 10):
            await projector.handle(
                [_reconciled(ticket_id=ticket_id, occurred_at=NOW + timedelta(minutes=minutes))]
            )

        history = await timeline.for_ticket(ticket_id, limit=10)

        assert [entry.occurred_at for entry in history] == [
            NOW + timedelta(minutes=10),
            NOW + timedelta(minutes=5),
            NOW,
        ]

    @pytest.mark.asyncio
    async def test_it_is_bounded(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        ticket_id = generate_uuid7()
        for minutes in range(5):
            await projector.handle(
                [_reconciled(ticket_id=ticket_id, occurred_at=NOW + timedelta(minutes=minutes))]
            )

        assert len(await timeline.for_ticket(ticket_id, limit=2)) == 2

    @pytest.mark.asyncio
    async def test_it_returns_nothing_for_a_ticket_recovery_never_touched(
        self, timeline: InMemoryReconciliationTimelineRepository
    ) -> None:
        assert await timeline.for_ticket(uuid4(), limit=10) == []

    @pytest.mark.asyncio
    async def test_the_pairing_query_is_honestly_empty(
        self,
        projector: ReconciliationTimelineProjector,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """`PairingReconciled` identifies a **ticket** — the reconciler may
        hold one half of a pair without the other — so `pairing_id` is null
        on every row rather than back-filled with a guess. The query exists
        so the caller does not change when the event grows the field."""
        await projector.handle([_reconciled()])

        assert timeline.entries[0].pairing_id is None
        assert await timeline.for_pairing(uuid4(), limit=10) == []
