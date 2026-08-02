"""`PairingService` — the four things only the orchestration can get wrong.

The service runs **for real** over in-memory storage and stubbed neighbours
(`tests/fakes/`), so the claim sequencing, the compensation, the
idempotency and the event boundary are all genuinely exercised. What is
substituted is the database, the clock, `friends` and `game` — nothing that
decides which pair is chosen, which is `PairingEngine`'s and is tested in
`test_pairing_engine.py`.

There is deliberately no duplication of the ordering and rating rules here.
This file asks: given that a pair was chosen, what happens to the two
tickets, and what happens when something fails.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.application.services import PairingService
from app.modules.matchmaking.domain.pairing import PairingEngine, RatingWindowPolicy
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType, Region
from app.modules.matchmaking.domain.queue_ticket import QueueStatus, QueueTicket
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.pairing import (
    ExplodingMatchCreation,
    RecordingMatchCreation,
    RefusingMatchCreation,
    StubExclusions,
    StubRecentOpponents,
)
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import InMemoryQueueRepository, RecordingPublisher

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)

#: A64-015.4's reservation window — the default
#: `MATCHMAKING_RESERVATION_TTL_SECONDS`, and two orders of magnitude inside
#: `TTL` so that nothing in this file confuses a stranded reservation with an
#: expired ticket.
RESERVATION_TTL = 30.0

POOL = QueuePool(variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED)
OTHER_POOL = QueuePool(
    variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED, region=Region.ASIA
)
CASUAL_POOL = QueuePool(variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.CASUAL)

#: Wide, because this file is not about the rating rule. A window that
#: refused a pair here would make every failure look like a compatibility
#: failure.
GENEROUS = RatingWindowPolicy(
    initial_points=1000, widen_every_seconds=30, widen_by_points=0, maximum_points=1000
)


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def tickets() -> InMemoryQueueRepository:
    return InMemoryQueueRepository()


@pytest.fixture
def exclusions() -> StubExclusions:
    return StubExclusions()


@pytest.fixture
def opponents() -> StubRecentOpponents:
    return StubRecentOpponents()


@pytest.fixture
def matches() -> RecordingMatchCreation:
    return RecordingMatchCreation()


@pytest.fixture
def events() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def unit_of_work() -> NullUnitOfWork:
    return NullUnitOfWork()


def _service(
    *,
    tickets: InMemoryQueueRepository,
    exclusions: StubExclusions,
    opponents: StubRecentOpponents,
    matches: object,
    events: RecordingPublisher,
    unit_of_work: NullUnitOfWork,
    clock: MovableClock,
    metrics: RecordingMetrics | None = None,
) -> PairingService:
    return PairingService(
        tickets=tickets,
        engine=PairingEngine(GENEROUS),
        exclusions=exclusions,
        opponents=opponents,
        matches=matches,  # type: ignore[arg-type]
        events=events,
        unit_of_work=unit_of_work,
        clock=clock,
        metrics=metrics if metrics is not None else RecordingMetrics(),
        candidate_batch_size=50,
        reservation_ttl_seconds=RESERVATION_TTL,
    )


@pytest.fixture
def service(
    tickets: InMemoryQueueRepository,
    exclusions: StubExclusions,
    opponents: StubRecentOpponents,
    matches: RecordingMatchCreation,
    events: RecordingPublisher,
    unit_of_work: NullUnitOfWork,
    clock: MovableClock,
) -> PairingService:
    return _service(
        tickets=tickets,
        exclusions=exclusions,
        opponents=opponents,
        matches=matches,
        events=events,
        unit_of_work=unit_of_work,
        clock=clock,
    )


def _queued(
    store: InMemoryQueueRepository,
    *,
    pool: QueuePool = POOL,
    rating: int = 1500,
    waited: float = 0.0,
    player_id: UUID | None = None,
) -> QueueTicket:
    """A waiting ticket, already in storage."""
    entered = NOW - timedelta(seconds=waited)
    ticket = QueueTicket(
        player_id=player_id or generate_uuid7(),
        pool=pool,
        rating_snapshot=rating,
        entered_at=entered,
        expires_at=entered + TTL,
    )
    store.tickets[ticket.id] = ticket
    return ticket


class TestAScanThatPairs:
    async def test_two_waiting_tickets_produce_a_match(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        _queued(tickets)
        _queued(tickets)

        outcome = await service.pair_once(pool=POOL)

        assert outcome.paired
        assert outcome.match_id is not None

    async def test_both_tickets_end_matched(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        one, other = _queued(tickets), _queued(tickets)

        await service.pair_once(pool=POOL)

        assert tickets.tickets[one.id].status is QueueStatus.MATCHED
        assert tickets.tickets[other.id].status is QueueStatus.MATCHED

    async def test_the_match_request_goes_through_game_public(
        self,
        service: PairingService,
        tickets: InMemoryQueueRepository,
        matches: RecordingMatchCreation,
    ) -> None:
        """§15.12. The request is a `game.public.CreateMatchRequest` and the
        service holds nothing else from `game` — the import boundary is
        asserted statically in `test_matchmaking_boundaries.py`; this is
        that boundary actually carrying a command."""
        one, other = _queued(tickets), _queued(tickets)

        await service.pair_once(pool=POOL)

        request = matches.requests[0]
        assert request.variant is ProductVariant.RUSSIAN_8X8
        assert {request.light.player_id, request.dark.player_id} == {
            one.player_id,
            other.player_id,
        }
        assert {request.light.queue_ticket_id, request.dark.queue_ticket_id} == {one.id, other.id}

    async def test_a_ranked_pool_asks_for_a_rated_match(
        self,
        service: PairingService,
        tickets: InMemoryQueueRepository,
        matches: RecordingMatchCreation,
    ) -> None:
        _queued(tickets)
        _queued(tickets)

        await service.pair_once(pool=POOL)

        assert matches.requests[0].rated is True

    async def test_a_casual_pool_does_not(
        self,
        service: PairingService,
        tickets: InMemoryQueueRepository,
        matches: RecordingMatchCreation,
    ) -> None:
        """`QueueType` collapsed to the one bit `game` acts on — a queue
        concept must not cross into a module that has no queue."""
        _queued(tickets, pool=CASUAL_POOL)
        _queued(tickets, pool=CASUAL_POOL)

        await service.pair_once(pool=CASUAL_POOL)

        assert matches.requests[0].rated is False

    async def test_the_request_stamps_the_engine_version(
        self,
        service: PairingService,
        tickets: InMemoryQueueRepository,
        matches: RecordingMatchCreation,
    ) -> None:
        """AD-15, read through `game.public` so `matchmaking` never imports
        the engine (R-2)."""
        _queued(tickets)
        _queued(tickets)

        await service.pair_once(pool=POOL)

        assert matches.requests[0].engine_version.as_primitive() >= 1

    async def test_pairing_publishes_one_event(
        self, service: PairingService, tickets: InMemoryQueueRepository, events: RecordingPublisher
    ) -> None:
        _queued(tickets)
        _queued(tickets)

        await service.pair_once(pool=POOL)

        assert events.types() == ["matchmaking.players_paired"]

    async def test_the_event_and_the_transitions_commit_together(
        self,
        service: PairingService,
        tickets: InMemoryQueueRepository,
        unit_of_work: NullUnitOfWork,
    ) -> None:
        """AD-16. Two commits — the claim, then the settle — and the event
        is inside the second, so a consumer cannot learn about a match whose
        tickets rolled back."""
        _queued(tickets)
        _queued(tickets)

        await service.pair_once(pool=POOL)

        assert unit_of_work.commits == 2


class TestAScanThatFindsNothing:
    async def test_an_empty_pool_is_idle(self, service: PairingService) -> None:
        outcome = await service.pair_once(pool=POOL)

        assert not outcome.paired
        assert outcome.scanned == 0

    async def test_one_waiting_ticket_is_idle(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        _queued(tickets)

        outcome = await service.pair_once(pool=POOL)

        assert not outcome.paired
        assert outcome.scanned == 1

    async def test_an_idle_scan_asks_nobody_about_exclusions(
        self, service: PairingService, tickets: InMemoryQueueRepository, exclusions: StubExclusions
    ) -> None:
        """One candidate cannot be a pair, so the cross-module reads are not
        worth making. A scan on an empty pool is the common case and must
        cost nothing."""
        _queued(tickets)

        await service.pair_once(pool=POOL)

        assert exclusions.calls == []

    async def test_an_idle_scan_publishes_nothing(
        self, service: PairingService, events: RecordingPublisher
    ) -> None:
        await service.pair_once(pool=POOL)

        assert events.published == []


class TestPoolIsolation:
    async def test_a_scan_reads_only_its_own_pool(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """§15.1. Two players in one pool and one in another; the scan sees
        two."""
        _queued(tickets, pool=POOL)
        _queued(tickets, pool=POOL)
        _queued(tickets, pool=OTHER_POOL)

        outcome = await service.pair_once(pool=POOL)

        assert outcome.scanned == 2

    async def test_players_in_different_pools_never_pair(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """§15.16. One player in each of two pools, identical ratings — the
        only thing keeping them apart is the pool."""
        _queued(tickets, pool=POOL)
        _queued(tickets, pool=OTHER_POOL)

        assert not (await service.pair_once(pool=POOL)).paired
        assert not (await service.pair_once(pool=OTHER_POOL)).paired

    async def test_a_ranked_player_never_meets_a_casual_one(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        _queued(tickets, pool=POOL)
        _queued(tickets, pool=CASUAL_POOL)

        assert not (await service.pair_once(pool=POOL)).paired

    async def test_an_untouched_pool_keeps_its_tickets_waiting(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        _queued(tickets, pool=POOL)
        _queued(tickets, pool=POOL)
        bystander = _queued(tickets, pool=OTHER_POOL)

        await service.pair_once(pool=POOL)

        assert tickets.tickets[bystander.id].status is QueueStatus.WAITING


class TestExclusionsAreBatched:
    async def test_the_block_read_is_one_call_for_the_whole_pool(
        self, service: PairingService, tickets: InMemoryQueueRepository, exclusions: StubExclusions
    ) -> None:
        """§15.6. Six candidates, one call — a per-candidate form would be
        the N+1 inside a job that runs several times a second."""
        for _ in range(6):
            _queued(tickets)

        await service.pair_once(pool=POOL)

        assert len(exclusions.calls) == 1
        assert len(exclusions.calls[0]) == 6

    async def test_the_recent_opponent_read_is_batched_too(
        self,
        service: PairingService,
        tickets: InMemoryQueueRepository,
        opponents: StubRecentOpponents,
    ) -> None:
        for _ in range(6):
            _queued(tickets)

        await service.pair_once(pool=POOL)

        assert len(opponents.calls) == 1

    async def test_a_blocked_pair_is_not_paired(
        self, service: PairingService, tickets: InMemoryQueueRepository, exclusions: StubExclusions
    ) -> None:
        """§15.5, end to end: the exclusion comes from `friends`' port and
        reaches the engine through the merge."""
        one, other = _queued(tickets), _queued(tickets)
        exclusions.block(one.player_id, other.player_id)

        outcome = await service.pair_once(pool=POOL)

        assert not outcome.paired
        assert tickets.tickets[one.id].status is QueueStatus.WAITING

    async def test_a_recent_opponent_is_not_paired_again(
        self,
        service: PairingService,
        tickets: InMemoryQueueRepository,
        opponents: StubRecentOpponents,
    ) -> None:
        """§15.7. The port is a no-op in production; what is asserted is
        that the seam holds — a non-empty answer really does veto."""
        one, other = _queued(tickets), _queued(tickets)
        opponents.played(one.player_id, other.player_id)

        assert not (await service.pair_once(pool=POOL)).paired

    async def test_a_blocked_pair_does_not_block_the_pool(
        self, service: PairingService, tickets: InMemoryQueueRepository, exclusions: StubExclusions
    ) -> None:
        """The scan falls through to the next candidate rather than
        reporting an empty pool — otherwise one block would stall a queue."""
        one = _queued(tickets, waited=300)
        blocked = _queued(tickets, waited=200)
        available = _queued(tickets, waited=100)
        exclusions.block(one.player_id, blocked.player_id)

        outcome = await service.pair_once(pool=POOL)

        assert outcome.paired
        assert tickets.tickets[available.id].status is QueueStatus.MATCHED


class _RaceLosingRepository(InMemoryQueueRepository):
    """A repository where another worker wins the claim.

    The snapshot returns both tickets — they were waiting when it ran — and
    by the time `claim_pair` executes, one has been reserved by somebody
    else. That is the only interleaving that produces a *lost claim* rather
    than a short snapshot, and it cannot be staged by writing to storage
    before the scan: the scan would simply not see the ticket.

    Models the outcome of `SKIP LOCKED`, not the lock. The lock is
    PostgreSQL's and is asserted with two real sessions in
    `tests/contract/test_queue_repository.py`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.claim_pair_calls = 0
        self.stolen: UUID | None = None

    async def claim_pair(
        self, ticket_ids: Sequence[UUID], *, now: datetime
    ) -> Sequence[QueueTicket]:
        self.claim_pair_calls += 1
        # Which of the two is stolen is not the test's to choose — the pair
        # is ordered by `TicketPair.of`, not by insertion — so it is
        # recorded and the assertions ask about *the other one*.
        stolen = self.tickets[ticket_ids[0]]
        self.stolen = stolen.id
        self.tickets[stolen.id] = stolen.reserved(until=NOW + timedelta(seconds=RESERVATION_TTL))
        return await super().claim_pair(ticket_ids, now=now)

    def survivor(self) -> QueueTicket:
        """The ticket the other worker did not take."""
        return next(ticket for ticket in self.tickets.values() if ticket.id != self.stolen)


class TestTheAtomicClaim:
    @pytest.fixture
    def contested(self) -> _RaceLosingRepository:
        return _RaceLosingRepository()

    @pytest.fixture
    def racing(
        self,
        contested: _RaceLosingRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        matches: RecordingMatchCreation,
        events: RecordingPublisher,
        unit_of_work: NullUnitOfWork,
        clock: MovableClock,
    ) -> PairingService:
        return _service(
            tickets=contested,
            exclusions=exclusions,
            opponents=opponents,
            matches=matches,
            events=events,
            unit_of_work=unit_of_work,
            clock=clock,
        )

    async def test_both_tickets_are_claimed_or_neither(
        self, racing: PairingService, contested: _RaceLosingRepository
    ) -> None:
        """§15.8. One of the two is taken between the snapshot and the
        claim. The other must be left exactly as it was — never
        half-claimed, never reserved on its own."""
        _queued(contested)
        _queued(contested)

        outcome = await racing.pair_once(pool=POOL)

        assert not outcome.paired
        assert contested.survivor().status is QueueStatus.WAITING

    async def test_a_lost_claim_is_reported_as_such(
        self, racing: PairingService, contested: _RaceLosingRepository
    ) -> None:
        """Distinguished from an idle pool, because an operator seeing it
        often knows two workers are scanning one pool more than they need
        to."""
        _queued(contested)
        _queued(contested)

        outcome = await racing.pair_once(pool=POOL)

        assert outcome.claim_lost
        assert contested.claim_pair_calls == 1

    async def test_a_lost_claim_creates_no_match(
        self,
        racing: PairingService,
        contested: _RaceLosingRepository,
        matches: RecordingMatchCreation,
    ) -> None:
        """The claim is what gates `game`. A scan that asked for a match
        before it held both tickets would create games for players who had
        already been paired elsewhere."""
        _queued(contested)
        _queued(contested)

        await racing.pair_once(pool=POOL)

        assert matches.requests == []

    async def test_a_lost_claim_publishes_nothing(
        self,
        racing: PairingService,
        contested: _RaceLosingRepository,
        events: RecordingPublisher,
    ) -> None:
        _queued(contested)
        _queued(contested)

        await racing.pair_once(pool=POOL)

        assert events.published == []

    async def test_a_reserved_ticket_is_not_offered_to_the_next_scan(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """The reservation's whole purpose: a second worker reading this
        pool a millisecond later sees two fewer waiting tickets and looks
        elsewhere, rather than selecting the same pair."""
        _queued(tickets)
        other = _queued(tickets)
        tickets.tickets[other.id] = tickets.tickets[other.id].reserved(
            until=NOW + timedelta(seconds=RESERVATION_TTL)
        )

        assert (await service.pair_once(pool=POOL)).scanned == 1

    async def test_a_ticket_cancelled_mid_scan_loses_the_pairing(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """§15.10 at the service level: a cancelled ticket is invisible to
        the snapshot, so its partner simply waits."""
        one, other = _queued(tickets), _queued(tickets)
        tickets.tickets[other.id] = tickets.tickets[other.id].cancelled(NOW)

        outcome = await service.pair_once(pool=POOL)

        assert not outcome.paired
        assert tickets.tickets[one.id].status is QueueStatus.WAITING

    async def test_an_expired_ticket_is_never_paired(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """§15.11."""
        one, other = _queued(tickets), _queued(tickets)
        tickets.tickets[other.id] = tickets.tickets[other.id].expired(NOW)

        assert not (await service.pair_once(pool=POOL)).paired
        assert tickets.tickets[one.id].status is QueueStatus.WAITING

    async def test_a_ticket_past_its_deadline_is_never_paired(
        self, service: PairingService, tickets: InMemoryQueueRepository, clock: MovableClock
    ) -> None:
        """Still `waiting` because no sweep has reached it. `expires_at` is
        the rule; the sweep is bookkeeping."""
        _queued(tickets, waited=0)
        _queued(tickets, waited=0)
        clock.advance(TTL.total_seconds() + 1)

        assert not (await service.pair_once(pool=POOL)).paired

    async def test_no_ticket_can_be_paired_twice(
        self,
        service: PairingService,
        tickets: InMemoryQueueRepository,
        matches: RecordingMatchCreation,
    ) -> None:
        """§15.9, as far as one process can assert it. Three players, so a
        second scan has a candidate left — and the two consumed by the first
        pairing must not appear in the second.

        The row lock itself is PostgreSQL's and is asserted in
        `tests/contract/test_queue_repository.py` with two real sessions.
        """
        for _ in range(3):
            _queued(tickets)

        first = await service.pair_once(pool=POOL)
        second = await service.pair_once(pool=POOL)

        assert first.paired
        assert not second.paired
        matched = [t for t in tickets.tickets.values() if t.status is QueueStatus.MATCHED]
        assert len(matched) == 2
        assert len(matches.requests) == 1


class TestCompensation:
    @pytest.fixture
    def refusing(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        events: RecordingPublisher,
        unit_of_work: NullUnitOfWork,
        clock: MovableClock,
    ) -> PairingService:
        return _service(
            tickets=tickets,
            exclusions=exclusions,
            opponents=opponents,
            matches=RefusingMatchCreation(),
            events=events,
            unit_of_work=unit_of_work,
            clock=clock,
        )

    @pytest.fixture
    def exploding(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        events: RecordingPublisher,
        unit_of_work: NullUnitOfWork,
        clock: MovableClock,
    ) -> PairingService:
        return _service(
            tickets=tickets,
            exclusions=exclusions,
            opponents=opponents,
            matches=ExplodingMatchCreation(),
            events=events,
            unit_of_work=unit_of_work,
            clock=clock,
        )

    async def test_a_refused_match_returns_both_tickets_to_waiting(
        self, refusing: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """§15.14 — the path a genuine refusal from `game` drives in
        production today."""
        one, other = _queued(tickets), _queued(tickets)

        outcome = await refusing.pair_once(pool=POOL)

        assert outcome.creation_refused
        assert tickets.tickets[one.id].status is QueueStatus.WAITING
        assert tickets.tickets[other.id].status is QueueStatus.WAITING

    async def test_a_released_ticket_keeps_its_place_in_line(
        self, refusing: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """A platform failure must not cost a player their wait. This is why
        `release` writes `status` and `resolved_at` and nothing else."""
        one = _queued(tickets, waited=300)
        _queued(tickets, waited=200)

        await refusing.pair_once(pool=POOL)

        assert tickets.tickets[one.id].entered_at == one.entered_at
        assert tickets.tickets[one.id].expires_at == one.expires_at

    async def test_a_released_ticket_carries_no_resolution_instant(
        self, refusing: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """A reservation is not an outcome. `resolved_at` set on a waiting
        ticket is a state the CHECK forbids and the aggregate refuses."""
        one, _other = _queued(tickets), _queued(tickets)

        await refusing.pair_once(pool=POOL)

        assert tickets.tickets[one.id].resolved_at is None

    async def test_a_released_player_is_paired_on_the_next_scan(
        self, refusing: PairingService, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """The point of compensation: nobody is lost from the queue."""
        _queued(tickets)
        _queued(tickets)
        await refusing.pair_once(pool=POOL)

        assert (await service.pair_once(pool=POOL)).paired

    async def test_a_refusal_publishes_nothing(
        self, refusing: PairingService, tickets: InMemoryQueueRepository, events: RecordingPublisher
    ) -> None:
        """Nothing durable happened. Announcing an abandoned attempt would
        push an implementation detail of a background job to every
        subscriber."""
        _queued(tickets)
        _queued(tickets)

        await refusing.pair_once(pool=POOL)

        assert events.published == []

    async def test_an_unexpected_failure_compensates_identically(
        self, exploding: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """A fault rather than a decision — an unreachable database, a bug
        in `game`. Two players are waiting either way, so the compensation
        must not depend on which."""
        one, other = _queued(tickets), _queued(tickets)

        outcome = await exploding.pair_once(pool=POOL)

        assert outcome.creation_refused
        assert tickets.tickets[one.id].status is QueueStatus.WAITING
        assert tickets.tickets[other.id].status is QueueStatus.WAITING

    async def test_a_scan_never_raises(
        self, exploding: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """It runs from a schedule, and an exception would stop the
        schedule — the argument `expire_due` and `OutboxRelay.run_once` both
        make."""
        _queued(tickets)
        _queued(tickets)

        await exploding.pair_once(pool=POOL)


class TestIdempotency:
    async def test_a_retry_of_one_pairing_creates_one_match(
        self,
        service: PairingService,
        tickets: InMemoryQueueRepository,
        matches: RecordingMatchCreation,
    ) -> None:
        """§15.15. The crash this models is a worker dying after `game`
        committed and before the tickets were settled: the tickets are still
        reserved, the retry re-derives the same `pairing_id`, and `game`
        returns the match it already has.
        """
        one, other = _queued(tickets), _queued(tickets)
        first = await service.pair_once(pool=POOL)

        # The settle is undone, leaving exactly the state a crash between
        # `game`'s commit and the settle would have left.
        tickets.tickets[one.id] = _reserved_again(tickets, one.id)
        tickets.tickets[other.id] = _reserved_again(tickets, other.id)

        replay = await matches.create_match(matches.requests[0])

        assert replay.match_id == first.match_id
        assert replay.created is False
        assert len(matches.requests) == 2

    async def test_the_pairing_id_is_the_only_key(
        self,
        service: PairingService,
        tickets: InMemoryQueueRepository,
        matches: RecordingMatchCreation,
    ) -> None:
        """Derived from the two ticket ids, so it survives a process
        restart with no stored state — §11 forbids relying on memory."""
        _queued(tickets)
        _queued(tickets)

        outcome = await service.pair_once(pool=POOL)

        assert matches.requests[0].pairing_id == outcome.pairing_id

    async def test_two_different_pairings_get_two_matches(
        self, service: PairingService, tickets: InMemoryQueueRepository
    ) -> None:
        """The other half: idempotency must not collapse distinct pairs."""
        _queued(tickets)
        _queued(tickets)
        first = await service.pair_once(pool=POOL)

        _queued(tickets)
        _queued(tickets)
        second = await service.pair_once(pool=POOL)

        assert first.match_id != second.match_id


def _reserved_again(store: InMemoryQueueRepository, ticket_id: UUID) -> QueueTicket:
    """A matched ticket rewound to `reserved`.

    Storage surgery, and the only place this file does any: it reconstructs
    the state a worker's death between `game`'s commit and the settle would
    have left, which no sequence of legal calls can produce from inside the
    service.
    """
    matched = store.tickets[ticket_id]
    return QueueTicket(
        id=matched.id,
        player_id=matched.player_id,
        pool=matched.pool,
        rating_snapshot=matched.rating_snapshot,
        entered_at=matched.entered_at,
        expires_at=matched.expires_at,
        status=QueueStatus.RESERVED,
        # A64-015.4: a reserved ticket carries the deadline both it and its
        # match were given. The rewind has to restore it, because the CHECK
        # and the aggregate both refuse a reservation without one — which is
        # exactly the property that lets the reconciler find this state.
        reserved_until=NOW + timedelta(seconds=RESERVATION_TTL),
    )
