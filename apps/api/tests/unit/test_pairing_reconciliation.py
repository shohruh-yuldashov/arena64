"""`PairingReconciliationService` — the four states it recognises, and the
action each implies (A64-015.4 §9).

A64-015.3 shipped pairing with a window in which a match exists and its
tickets do not say so, and left the repair to a human reading an `ERROR`
log line. This file is the evidence that it is no longer a human's job.

The service runs **for real** over in-memory storage and stubbed neighbours
(`tests/fakes/`), so the claim sequencing, the decision table, the
idempotency and the event boundary are all genuinely exercised. What is
substituted is the database, the clock and `game`.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.game.public import PairingSettlement, ProductVariant
from app.modules.matchmaking.application.services import PairingReconciliationService
from app.modules.matchmaking.domain.events import ReconciliationAction
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType
from app.modules.matchmaking.domain.queue_ticket import QueueStatus, QueueTicket
from tests.fakes.matches import StubAcceptanceExpiry, StubSettlements
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import InMemoryQueueRepository, RecordingPublisher
from tests.fakes.time_controls import BLITZ

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)

#: The default `MATCHMAKING_RESERVATION_TTL_SECONDS` — two orders of
#: magnitude inside `TTL`, which is the whole point of §5: a stranded
#: reservation is recovered in seconds rather than after the ten minutes
#: A64-015.3 left it to.
RESERVATION = timedelta(seconds=30)

POOL = QueuePool(
    variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED, time_control_id=BLITZ.id
)


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def tickets() -> InMemoryQueueRepository:
    return InMemoryQueueRepository()


@pytest.fixture
def settlements() -> StubSettlements:
    return StubSettlements()


@pytest.fixture
def acceptance() -> StubAcceptanceExpiry:
    return StubAcceptanceExpiry()


@pytest.fixture
def events() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def service(
    tickets: InMemoryQueueRepository,
    settlements: StubSettlements,
    acceptance: StubAcceptanceExpiry,
    events: RecordingPublisher,
    clock: MovableClock,
) -> PairingReconciliationService:
    return PairingReconciliationService(
        tickets=tickets,
        settlements=settlements,
        acceptance=acceptance,
        events=events,
        unit_of_work=NullUnitOfWork(),
        clock=clock,
        metrics=RecordingMetrics(),
        batch_size=50,
    )


def _reserved(
    store: InMemoryQueueRepository,
    *,
    entered: datetime = NOW,
    reserved_at: datetime = NOW,
    ttl: timedelta = TTL,
) -> QueueTicket:
    """A ticket a worker claimed and then died holding.

    Written straight into storage rather than through `PairingService`,
    because the state under test is precisely the one no sequence of legal
    calls leaves behind: a reservation whose owner is gone.
    """
    ticket = QueueTicket(
        player_id=generate_uuid7(),
        pool=POOL,
        time_control=BLITZ,
        rating_snapshot=1500,
        entered_at=entered,
        expires_at=entered + ttl,
        status=QueueStatus.RESERVED,
        reserved_until=reserved_at + RESERVATION,
    )
    store.tickets[ticket.id] = ticket
    return ticket


def _settlement(created_at: datetime = NOW) -> PairingSettlement:
    return PairingSettlement(
        match_id=generate_uuid7(), pairing_id=generate_uuid7(), created_at=created_at
    )


def _lapse(clock: MovableClock) -> None:
    """Move past the reservation deadline, and nowhere near the ticket's."""
    clock.advance(RESERVATION.total_seconds() + 1)


def _actions(events: RecordingPublisher) -> list[ReconciliationAction]:
    return [event.action for event in events.published]  # type: ignore[attr-defined]


class TestAMatchThatWasCreatedButNotSettled:
    """§9's first case, and the one A64-015.3 recorded as needing a human:
    `game` committed, the worker died, and two tickets still say
    `reserved`."""

    async def test_the_ticket_is_settled_as_matched(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        settlements: StubSettlements,
        clock: MovableClock,
    ) -> None:
        ticket = _reserved(tickets)
        settlements.record(ticket.id, _settlement())
        _lapse(clock)

        outcome = await service.reconcile_once()

        assert outcome.settled == 1
        assert tickets.tickets[ticket.id].status is QueueStatus.MATCHED

    async def test_it_records_when_the_match_was_created(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        settlements: StubSettlements,
        clock: MovableClock,
    ) -> None:
        """`resolved_at` answers "when did this player's game start", and it
        must not become "when did the reconciler get round to it" just
        because a worker died in between."""
        ticket = _reserved(tickets)
        created_at = NOW + timedelta(seconds=2)
        settlements.record(ticket.id, _settlement(created_at))
        clock.advance(600)

        await service.reconcile_once()

        assert tickets.tickets[ticket.id].resolved_at == created_at

    async def test_the_settlement_is_announced_with_its_match(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        settlements: StubSettlements,
        events: RecordingPublisher,
        clock: MovableClock,
    ) -> None:
        ticket = _reserved(tickets)
        settlement = _settlement()
        settlements.record(ticket.id, settlement)
        _lapse(clock)

        await service.reconcile_once()

        assert events.types() == ["matchmaking.pairing_reconciled"]
        assert _actions(events) == [ReconciliationAction.SETTLED]
        assert events.published[0].match_id == settlement.match_id  # type: ignore[attr-defined]

    async def test_the_settlement_read_is_batched(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
        settlements: StubSettlements,
    ) -> None:
        """One call for the claim rather than one per ticket — the N+1 a
        recovery job is the worst place on the platform to contain."""
        for _ in range(4):
            _reserved(tickets)
        _lapse(clock)

        await service.reconcile_once()

        assert len(settlements.calls) == 1
        assert len(settlements.calls[0]) == 4


class TestAnOrphanedReservationWithNoMatch:
    """§9's second case: the worker died *before* it reached `game`, so
    there is nothing to settle and the player should go back in line."""

    async def test_the_ticket_returns_to_waiting(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
    ) -> None:
        ticket = _reserved(tickets)
        _lapse(clock)

        outcome = await service.reconcile_once()

        assert outcome.released == 1
        assert tickets.tickets[ticket.id].status is QueueStatus.WAITING

    async def test_the_player_keeps_their_place_in_line(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
    ) -> None:
        """A failure that was the platform's must not cost a player their
        wait — the same rule `PairingService`'s compensation follows."""
        ticket = _reserved(tickets, entered=NOW - timedelta(minutes=3))
        _lapse(clock)

        await service.reconcile_once()

        released = tickets.tickets[ticket.id]
        assert released.entered_at == ticket.entered_at
        assert released.expires_at == ticket.expires_at

    async def test_the_released_ticket_carries_no_deadline(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
    ) -> None:
        """A stale `reserved_until` on a waiting row is a ticket the next
        tick believes is a stranded reservation — which is how a recovery
        job becomes a loop that releases the same player forever."""
        ticket = _reserved(tickets)
        _lapse(clock)

        await service.reconcile_once()

        assert tickets.tickets[ticket.id].reserved_until is None

    async def test_the_release_is_announced(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        events: RecordingPublisher,
        clock: MovableClock,
    ) -> None:
        _reserved(tickets)
        _lapse(clock)

        await service.reconcile_once()

        assert _actions(events) == [ReconciliationAction.RELEASED]
        assert events.published[0].match_id is None  # type: ignore[attr-defined]

    async def test_a_reservation_inside_its_window_is_left_alone(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        events: RecordingPublisher,
    ) -> None:
        """A live worker is mid-pairing. Releasing its tickets would make
        the reconciler the thing that breaks pairings."""
        ticket = _reserved(tickets)

        outcome = await service.reconcile_once()

        assert outcome.claimed == 0
        assert tickets.tickets[ticket.id].status is QueueStatus.RESERVED
        assert events.published == []


class TestAReservationWhoseTicketAlsoFellDue:
    """§9's third case: no match, and the player's own ten-minute window
    closed while the reservation was stranded."""

    async def test_the_ticket_expires_rather_than_returning_to_waiting(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
    ) -> None:
        """Releasing it would put somebody back in a queue that had already
        stopped considering them, with QT-1's index still holding their
        slot."""
        ticket = _reserved(tickets, entered=NOW - TTL + timedelta(seconds=5))
        _lapse(clock)

        outcome = await service.reconcile_once()

        assert outcome.expired == 1
        assert tickets.tickets[ticket.id].status is QueueStatus.EXPIRED

    async def test_the_expiry_is_announced(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        events: RecordingPublisher,
        clock: MovableClock,
    ) -> None:
        _reserved(tickets, entered=NOW - TTL + timedelta(seconds=5))
        _lapse(clock)

        await service.reconcile_once()

        assert _actions(events) == [ReconciliationAction.EXPIRED]

    async def test_a_settled_ticket_is_matched_even_past_its_own_deadline(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        settlements: StubSettlements,
        clock: MovableClock,
    ) -> None:
        """The match wins. A player whose game exists must not be recorded
        as having timed out of the queue that produced it."""
        ticket = _reserved(tickets, entered=NOW - TTL + timedelta(seconds=5))
        settlements.record(ticket.id, _settlement())
        _lapse(clock)

        await service.reconcile_once()

        assert tickets.tickets[ticket.id].status is QueueStatus.MATCHED


class TestExpiringUnansweredMatches:
    """§9's fourth case, delegated to `game` through its published sweep."""

    async def test_the_pass_drives_game_s_own_expiry(
        self, service: PairingReconciliationService, acceptance: StubAcceptanceExpiry
    ) -> None:
        acceptance.expired = [generate_uuid7(), generate_uuid7()]

        outcome = await service.reconcile_once()

        assert outcome.expired_matches == 2
        assert acceptance.calls == [50]

    async def test_a_pass_with_nothing_to_do_is_idle(
        self, service: PairingReconciliationService
    ) -> None:
        outcome = await service.reconcile_once()

        assert outcome.is_idle

    async def test_an_unreachable_game_does_not_stop_ticket_recovery(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        acceptance: StubAcceptanceExpiry,
        clock: MovableClock,
    ) -> None:
        """Two halves, two failure domains. A `game` that cannot expire its
        matches must not leave this module's own reservations stranded as
        well."""
        acceptance.fails = True
        ticket = _reserved(tickets)
        _lapse(clock)

        outcome = await service.reconcile_once()

        assert outcome.expired_matches == 0
        assert tickets.tickets[ticket.id].status is QueueStatus.WAITING


class TestIdempotency:
    async def test_a_second_pass_changes_nothing(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        settlements: StubSettlements,
        events: RecordingPublisher,
        clock: MovableClock,
    ) -> None:
        """Duplicate task delivery is a certainty under AD-17's
        at-least-once contract. Running this twice must be running it
        once."""
        settled = _reserved(tickets)
        settlements.record(settled.id, _settlement())
        # A second reservation with no match, so the pass exercises both
        # actions and the re-run has both to get wrong.
        _reserved(tickets)
        _lapse(clock)

        first = await service.reconcile_once()
        second = await service.reconcile_once()

        assert (first.settled, first.released) == (1, 1)
        assert second.claimed == 0
        assert len(events.published) == 2

    async def test_a_ticket_somebody_else_reconciled_is_not_reconciled_twice(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        settlements: StubSettlements,
        events: RecordingPublisher,
        clock: MovableClock,
    ) -> None:
        """Two reconcilers race after the claim commits. The compare-and-set
        makes the loser's write apply to nothing, and it must not announce a
        transition it did not make."""
        ticket = _reserved(tickets)
        settlements.record(ticket.id, _settlement())
        _lapse(clock)
        # The other worker got there first.
        tickets.tickets[ticket.id] = ticket.matched(NOW)

        outcome = await service.reconcile_once()

        assert outcome.settled == 0
        assert events.published == []

    async def test_a_failed_settlement_read_leaves_everything_reserved(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        settlements: StubSettlements,
        events: RecordingPublisher,
        clock: MovableClock,
    ) -> None:
        """There is no safe default: guessing "no match" releases a ticket
        whose player already has a game, and guessing "matched" strands one
        who does not. The tick fails and the next one claims again."""
        ticket = _reserved(tickets)
        settlements.fails = True
        _lapse(clock)

        outcome = await service.reconcile_once()

        assert (outcome.settled, outcome.released, outcome.expired) == (0, 0, 0)
        assert tickets.tickets[ticket.id].status is QueueStatus.RESERVED
        assert events.published == []

    async def test_a_pass_never_raises(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        settlements: StubSettlements,
        acceptance: StubAcceptanceExpiry,
        clock: MovableClock,
    ) -> None:
        """It runs from a schedule, and a tick that propagated would stop
        the schedule — the argument every background job on this platform
        makes."""
        settlements.fails = True
        acceptance.fails = True
        _reserved(tickets)
        _lapse(clock)

        await service.reconcile_once()


class TestTheBatchIsBounded:
    async def test_a_pass_claims_no_more_than_its_batch(
        self,
        tickets: InMemoryQueueRepository,
        settlements: StubSettlements,
        acceptance: StubAcceptanceExpiry,
        events: RecordingPublisher,
        clock: MovableClock,
    ) -> None:
        """CLAUDE.md §10.5. The interesting case is a rolling restart, which
        strands a burst of reservations at once."""
        service = PairingReconciliationService(
            tickets=tickets,
            settlements=settlements,
            acceptance=acceptance,
            events=events,
            unit_of_work=NullUnitOfWork(),
            clock=clock,
            metrics=RecordingMetrics(),
            batch_size=2,
        )
        for _ in range(5):
            _reserved(tickets)
        _lapse(clock)

        outcome = await service.reconcile_once()

        assert outcome.claimed == 2

    async def test_repeated_passes_drain_the_backlog(
        self,
        service: PairingReconciliationService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
    ) -> None:
        ids: list[UUID] = [_reserved(tickets).id for _ in range(3)]
        _lapse(clock)

        await service.reconcile_once()

        assert all(tickets.tickets[t].status is QueueStatus.WAITING for t in ids)
