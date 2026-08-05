"""`QueueService` — the four use cases A64-014.1 requires tested: join,
duplicate rejected, leave, expiration.

The service runs **for real** over in-memory storage (`tests/fakes/
queue_repository.py`), so the presence rule, the read-first duplicate check,
the transaction sequencing and the expiry arithmetic are all genuinely
exercised. What is substituted is the database, the clock and Redis —
nothing that decides anything.

Time is a `MovableClock` rather than a fixed one, because expiry is the
whole point of half this file: asserting that a ten-minute ticket lapses
means moving past its window, and AD-07 exists precisely so that does not
mean sleeping.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.application.eligibility import PresenceEligibilityPolicy
from app.modules.matchmaking.application.services import QueueService
from app.modules.matchmaking.domain.exceptions import AlreadyQueued, QueueNotPermitted
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType, Region
from app.modules.matchmaking.domain.queue_ticket import QueueStatus
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import (
    FixedRatingProvider,
    InMemoryQueueRepository,
    RecordingPublisher,
    StubPresence,
)
from tests.fakes.time_controls import BLITZ, FakeTimeControlCatalogue

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
TTL_SECONDS = 600
PLAYER = generate_uuid7()


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def tickets() -> InMemoryQueueRepository:
    return InMemoryQueueRepository()


def pool(queue_type: QueueType, region: Region = Region.GLOBAL) -> QueuePool:
    """A pool for the one variant Arena64 offers."""
    return QueuePool(
        variant=ProductVariant.RUSSIAN_8X8,
        queue_type=queue_type,
        region=region,
        time_control_id=BLITZ.id,
    )


@pytest.fixture
def presence() -> StubPresence:
    return StubPresence()


@pytest.fixture
def events() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def unit_of_work() -> NullUnitOfWork:
    return NullUnitOfWork()


@pytest.fixture
def service(
    tickets: InMemoryQueueRepository,
    presence: StubPresence,
    events: RecordingPublisher,
    unit_of_work: NullUnitOfWork,
    clock: MovableClock,
) -> QueueService:
    return QueueService(
        time_controls=FakeTimeControlCatalogue(),
        tickets=tickets,
        ratings=FixedRatingProvider(1500),
        eligibility=PresenceEligibilityPolicy(presence),
        events=events,
        unit_of_work=unit_of_work,
        clock=clock,
        ticket_ttl_seconds=TTL_SECONDS,
        snapshot_limit=200,
    )


async def _join(service: QueueService, player_id: UUID = PLAYER) -> None:
    await service.join(player_id=player_id, pool=pool(QueueType.RANKED, Region.EUROPE))


class TestJoin:
    async def test_joining_issues_a_waiting_ticket(
        self, service: QueueService, clock: MovableClock
    ) -> None:
        ticket = await service.join(player_id=PLAYER, pool=pool(QueueType.RANKED, Region.EUROPE))

        assert ticket.status is QueueStatus.WAITING
        assert ticket.player_id == PLAYER
        assert ticket.queue_type is QueueType.RANKED
        assert ticket.region is Region.EUROPE
        assert ticket.expires_at == NOW + timedelta(seconds=TTL_SECONDS)

    async def test_the_ticket_records_the_rating_the_provider_gave(
        self,
        tickets: InMemoryQueueRepository,
        presence: StubPresence,
        events: RecordingPublisher,
        unit_of_work: NullUnitOfWork,
        clock: MovableClock,
    ) -> None:
        """QT-2, and asserted against a *chosen* number rather than the
        domain's fallback — otherwise the test would pass with the rating
        hardcoded in two places."""
        service = QueueService(
            time_controls=FakeTimeControlCatalogue(),
            tickets=tickets,
            ratings=FixedRatingProvider(2140),
            eligibility=PresenceEligibilityPolicy(presence),
            events=events,
            unit_of_work=unit_of_work,
            clock=clock,
            ticket_ttl_seconds=TTL_SECONDS,
            snapshot_limit=200,
        )

        ticket = await service.join(player_id=PLAYER, pool=pool(QueueType.RANKED, Region.GLOBAL))

        assert ticket.rating_snapshot == 2140

    async def test_joining_publishes_an_enqueued_event(
        self, service: QueueService, events: RecordingPublisher
    ) -> None:
        await _join(service)

        assert events.types() == ["matchmaking.queue_ticket_enqueued"]

    async def test_the_event_and_the_ticket_commit_together(
        self, service: QueueService, unit_of_work: NullUnitOfWork
    ) -> None:
        """AD-16: one transaction, one commit. Two would mean a pairing
        worker could learn about a ticket that rolled back."""
        await _join(service)

        assert unit_of_work.commits == 1

    async def test_a_player_recorded_offline_is_refused(
        self, service: QueueService, presence: StubPresence
    ) -> None:
        """A *recorded* sign-out is the platform's only positive evidence of
        absence, and it is the only thing that refuses a join."""
        presence.offline(PLAYER, at=NOW)

        with pytest.raises(QueueNotPermitted):
            await _join(service)

    async def test_a_player_recorded_online_is_admitted(
        self, service: QueueService, presence: StubPresence
    ) -> None:
        presence.online(PLAYER, at=NOW)

        await _join(service)

    async def test_unknown_presence_is_admitted(self, service: QueueService) -> None:
        """`None` collapses an expired window, an unrecorded player and an
        unreachable Redis. Refusing on it would make a cache blip an outage
        of matchmaking (system-design.md T-2)."""
        await _join(service)

    async def test_a_refused_join_writes_nothing(
        self,
        service: QueueService,
        presence: StubPresence,
        tickets: InMemoryQueueRepository,
        events: RecordingPublisher,
    ) -> None:
        presence.offline(PLAYER, at=NOW)

        with pytest.raises(QueueNotPermitted):
            await _join(service)

        assert tickets.tickets == {}
        assert events.published == []


class TestDuplicateJoin:
    async def test_a_second_join_is_refused(self, service: QueueService) -> None:
        """QT-1. One live ticket per player."""
        await _join(service)

        with pytest.raises(AlreadyQueued):
            await _join(service)

    async def test_a_second_join_in_a_different_pool_is_refused(
        self, service: QueueService
    ) -> None:
        """**Across all pools**, which is the half of QT-1 an index keyed on
        `(player_id, queue_type)` would silently permit — and multi-queueing
        is what pairs somebody into two simultaneous matches."""
        await _join(service)

        with pytest.raises(AlreadyQueued):
            await service.join(player_id=PLAYER, pool=pool(QueueType.CASUAL, Region.ASIA))

    async def test_a_refused_duplicate_publishes_nothing(
        self, service: QueueService, events: RecordingPublisher
    ) -> None:
        await _join(service)

        with pytest.raises(AlreadyQueued):
            await _join(service)

        assert events.types() == ["matchmaking.queue_ticket_enqueued"]

    async def test_another_player_may_join_the_same_pool(
        self, service: QueueService, tickets: InMemoryQueueRepository
    ) -> None:
        """The constraint is per player, not per pool — a check that
        accidentally keyed on the pool would pass every test above and fail
        this one."""
        await _join(service)
        await _join(service, generate_uuid7())

        assert len(tickets.tickets) == 2

    async def test_a_player_may_re_queue_after_leaving(self, service: QueueService) -> None:
        """The uniqueness rule is on the *live* state. A plain unique index
        would mean a player could queue once, ever."""
        await _join(service)
        await service.leave(player_id=PLAYER)

        await _join(service)


class TestLeave:
    async def test_leaving_cancels_the_ticket(
        self, service: QueueService, tickets: InMemoryQueueRepository, clock: MovableClock
    ) -> None:
        ticket = await service.join(player_id=PLAYER, pool=pool(QueueType.RANKED, Region.EUROPE))
        clock.advance(30)

        assert await service.leave(player_id=PLAYER) is True

        stored = tickets.tickets[ticket.id]
        assert stored.status is QueueStatus.CANCELLED
        assert stored.resolved_at == NOW + timedelta(seconds=30)

    async def test_leaving_publishes_a_cancelled_event_carrying_the_wait(
        self, service: QueueService, events: RecordingPublisher, clock: MovableClock
    ) -> None:
        await _join(service)
        clock.advance(45)

        await service.leave(player_id=PLAYER)

        cancelled = events.published[-1]
        assert type(cancelled).event_type == "matchmaking.queue_ticket_cancelled"
        assert cancelled.waited_for_seconds == pytest.approx(45.0)  # type: ignore[attr-defined]

    async def test_leaving_when_not_queued_is_idempotent(
        self, service: QueueService, events: RecordingPublisher
    ) -> None:
        """`DELETE` semantics, and one answer for both cases — so the status
        code never reports queue state back to a probe."""
        assert await service.leave(player_id=PLAYER) is False
        assert events.published == []

    async def test_leaving_twice_publishes_one_event(
        self, service: QueueService, events: RecordingPublisher
    ) -> None:
        await _join(service)

        await service.leave(player_id=PLAYER)
        await service.leave(player_id=PLAYER)

        assert events.types().count("matchmaking.queue_ticket_cancelled") == 1

    async def test_leaving_an_already_expired_ticket_reports_nothing_to_do(
        self, service: QueueService, clock: MovableClock
    ) -> None:
        """The ticket is still `waiting` in storage — the sweeper has not
        run — but it is past its deadline, so `active_ticket` reports absent
        and the player is told they were not queued. Which is true."""
        await _join(service)
        clock.advance(TTL_SECONDS + 1)

        assert await service.leave(player_id=PLAYER) is False


class TestActiveTicket:
    async def test_a_live_ticket_is_reported(self, service: QueueService) -> None:
        ticket = await service.join(player_id=PLAYER, pool=pool(QueueType.RANKED, Region.EUROPE))

        found = await service.active_ticket(player_id=PLAYER)

        assert found is not None
        assert found.id == ticket.id

    async def test_no_ticket_is_reported_for_a_player_who_never_queued(
        self, service: QueueService
    ) -> None:
        assert await service.active_ticket(player_id=PLAYER) is None

    async def test_a_due_ticket_reads_as_absent_before_the_sweep(
        self, service: QueueService, clock: MovableClock
    ) -> None:
        """`expires_at` is the rule and the sweep is bookkeeping. A player
        must never be told they are queued because a worker is behind."""
        await _join(service)
        clock.advance(TTL_SECONDS)

        assert await service.active_ticket(player_id=PLAYER) is None


class TestSnapshot:
    async def test_a_snapshot_counts_only_its_own_pool(self, service: QueueService) -> None:
        await _join(service, generate_uuid7())
        await _join(service, generate_uuid7())
        await service.join(player_id=generate_uuid7(), pool=pool(QueueType.CASUAL, Region.EUROPE))

        snapshot = await service.snapshot(pool=pool(QueueType.RANKED, Region.EUROPE))

        assert snapshot.waiting == 2

    async def test_a_snapshot_excludes_due_tickets(
        self, service: QueueService, clock: MovableClock
    ) -> None:
        await _join(service, generate_uuid7())
        clock.advance(TTL_SECONDS)
        await _join(service, generate_uuid7())

        snapshot = await service.snapshot(pool=pool(QueueType.RANKED, Region.EUROPE))

        assert snapshot.waiting == 1

    async def test_the_page_is_bounded_while_the_depth_is_not(
        self,
        tickets: InMemoryQueueRepository,
        presence: StubPresence,
        events: RecordingPublisher,
        unit_of_work: NullUnitOfWork,
        clock: MovableClock,
    ) -> None:
        """`QueueSnapshot` keeps the two apart on purpose: a bounded read
        must not turn into a wrong number, and `len(tickets)` is exactly the
        mistake that would."""
        service = QueueService(
            time_controls=FakeTimeControlCatalogue(),
            tickets=tickets,
            ratings=FixedRatingProvider(),
            eligibility=PresenceEligibilityPolicy(presence),
            events=events,
            unit_of_work=unit_of_work,
            clock=clock,
            ticket_ttl_seconds=TTL_SECONDS,
            snapshot_limit=2,
        )
        for _ in range(5):
            await _join(service, generate_uuid7())

        snapshot = await service.snapshot(pool=pool(QueueType.RANKED, Region.EUROPE))

        assert snapshot.waiting == 5
        assert len(snapshot.tickets) == 2


class TestExpiry:
    async def test_a_due_ticket_is_expired(
        self, service: QueueService, tickets: InMemoryQueueRepository, clock: MovableClock
    ) -> None:
        ticket = await service.join(player_id=PLAYER, pool=pool(QueueType.RANKED, Region.EUROPE))
        clock.advance(TTL_SECONDS)

        sweep = await service.expire_due(limit=50, claimed_by="w1")

        assert sweep.claimed == 1
        assert sweep.expired == 1
        assert tickets.tickets[ticket.id].status is QueueStatus.EXPIRED

    async def test_a_ticket_that_is_not_yet_due_is_left_alone(
        self, service: QueueService, clock: MovableClock
    ) -> None:
        await _join(service)
        clock.advance(TTL_SECONDS - 1)

        sweep = await service.expire_due(limit=50, claimed_by="w1")

        assert sweep.is_idle

    async def test_expiring_publishes_an_event_dated_to_the_deadline(
        self, service: QueueService, events: RecordingPublisher, clock: MovableClock
    ) -> None:
        """`occurred_at` is the ticket's own `expires_at`, not the sweep's
        instant: the fact became true when the window closed, and the
        outbox orders by causation (database.md §12.5)."""
        ticket = await service.join(player_id=PLAYER, pool=pool(QueueType.RANKED, Region.EUROPE))
        clock.advance(TTL_SECONDS + 120)

        await service.expire_due(limit=50, claimed_by="w1")

        expired = events.published[-1]
        assert type(expired).event_type == "matchmaking.queue_ticket_expired"
        assert expired.occurred_at == ticket.expires_at

    async def test_an_expired_player_may_queue_again(
        self, service: QueueService, clock: MovableClock
    ) -> None:
        await _join(service)
        clock.advance(TTL_SECONDS)
        await service.expire_due(limit=50, claimed_by="w1")

        await _join(service)

    async def test_the_sweep_is_bounded_by_its_limit(
        self, service: QueueService, clock: MovableClock
    ) -> None:
        """CLAUDE.md §10.5. The interesting case is a queue that filled
        while the sweeper was down."""
        for _ in range(5):
            await _join(service, generate_uuid7())
        clock.advance(TTL_SECONDS)

        sweep = await service.expire_due(limit=2, claimed_by="w1")

        assert sweep.claimed == 2

    async def test_a_cancelled_ticket_is_never_expired_on_top(
        self, service: QueueService, tickets: InMemoryQueueRepository, clock: MovableClock
    ) -> None:
        """The `status = 'waiting'` predicate on both writes. Without it a
        sweep would re-stamp a cancellation as an expiry, and the row would
        report a departure the player did not make."""
        ticket = await service.join(player_id=PLAYER, pool=pool(QueueType.RANKED, Region.EUROPE))
        await service.leave(player_id=PLAYER)
        clock.advance(TTL_SECONDS)

        sweep = await service.expire_due(limit=50, claimed_by="w1")

        assert sweep.is_idle
        assert tickets.tickets[ticket.id].status is QueueStatus.CANCELLED

    async def test_sweeping_the_same_ticket_twice_expires_it_once(
        self, service: QueueService, tickets: InMemoryQueueRepository, clock: MovableClock
    ) -> None:
        """A64-015.2 §9: repeated expiration must be safe. The sweep is a
        periodic task, so it *will* run again over a table whose due rows
        it has already resolved — and a worker that died between its claim
        and its commit leaves rows a later tick claims for a second time.
        """
        ticket = await service.join(player_id=PLAYER, pool=pool(QueueType.RANKED, Region.EUROPE))
        clock.advance(TTL_SECONDS)
        await service.expire_due(limit=50, claimed_by="w1")

        second = await service.expire_due(limit=50, claimed_by="w1")

        assert second.is_idle
        assert tickets.tickets[ticket.id].status is QueueStatus.EXPIRED

    async def test_a_second_sweep_publishes_no_second_event(
        self, service: QueueService, events: RecordingPublisher, clock: MovableClock
    ) -> None:
        """The consequence that matters. A duplicate `QueueTicketExpired`
        is a subscriber told twice that a player left a queue they were
        already out of — and once pairing exists, a second event for a
        ticket that has since been matched."""
        await _join(service)
        clock.advance(TTL_SECONDS)
        await service.expire_due(limit=50, claimed_by="w1")
        published_after_one_sweep = len(events.published)

        await service.expire_due(limit=50, claimed_by="w1")

        assert len(events.published) == published_after_one_sweep

    async def test_a_ticket_expired_by_one_worker_is_not_re_expired_by_another(
        self, service: QueueService, tickets: InMemoryQueueRepository, clock: MovableClock
    ) -> None:
        """Horizontal workers, which is what `FOR UPDATE SKIP LOCKED`
        exists for. Sequenced here rather than raced, because the fake
        cannot hold a row lock — the contract test against real PostgreSQL
        is what exercises the locking itself."""
        ticket = await service.join(player_id=PLAYER, pool=pool(QueueType.RANKED, Region.EUROPE))
        clock.advance(TTL_SECONDS)
        await service.expire_due(limit=50, claimed_by="w1")

        sweep = await service.expire_due(limit=50, claimed_by="w2")

        assert sweep.expired == 0
        assert tickets.tickets[ticket.id].resolved_at == ticket.expires_at

    async def test_an_idle_sweep_publishes_nothing(
        self, service: QueueService, events: RecordingPublisher
    ) -> None:
        sweep = await service.expire_due(limit=50, claimed_by="w1")

        assert sweep.is_idle
        assert events.published == []

    async def test_the_claim_and_the_resolutions_are_two_transactions(
        self, service: QueueService, unit_of_work: NullUnitOfWork, clock: MovableClock
    ) -> None:
        """The claim commits on its own so the rows are visibly taken before
        any event is written — a second sweeper polling mid-batch can only
        skip them if the claim is committed."""
        await _join(service)
        clock.advance(TTL_SECONDS)
        commits_before = unit_of_work.commits

        await service.expire_due(limit=50, claimed_by="w1")

        assert unit_of_work.commits - commits_before == 2
