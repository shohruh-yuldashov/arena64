"""QT-3's rematch guard, end to end through `game.public` — A64-015.4 §11.

A64-015.3 declared `RecentOpponentProvider`, shipped `NoRecentOpponents`
against it, and predicted that the real implementation would be "satisfied
by `game.public` and no use case, no engine and no test changes". Two things
are asserted here: that the read works, and that the prediction held —
`PairingService` takes `GameRecentOpponents` with no adapter, because the
published port and the consumer's port have the same shape on purpose.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION
from app.modules.game.application.services import GameRecentOpponents
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.public import ProductVariant, RecentOpponentReader
from app.modules.matchmaking.application.ports import RecentOpponentProvider
from app.modules.matchmaking.application.services import PairingService
from app.modules.matchmaking.domain.pairing import PairingEngine, RatingWindowPolicy
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType
from app.modules.matchmaking.domain.queue_ticket import QueueStatus, QueueTicket
from app.platform.metrics import NullMetrics
from tests.fakes.matches import InMemoryMatchRecordRepository
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.pairing import RecordingMatchCreation, StubExclusions, StubRatings
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import InMemoryQueueRepository, RecordingPublisher
from tests.fakes.time_controls import BLITZ

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)
WINDOW = timedelta(seconds=30)

POOL = QueuePool(
    variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED, time_control_id=BLITZ.id
)

#: Wide, so nothing in this file fails for a rating reason — the rule under
#: test is the exclusion.
GENEROUS = RatingWindowPolicy(
    initial_points=1000, widen_every_seconds=30, widen_by_points=0, maximum_points=1000
)


@pytest.fixture
def matches() -> InMemoryMatchRecordRepository:
    return InMemoryMatchRecordRepository()


@pytest.fixture
def opponents(matches: InMemoryMatchRecordRepository) -> GameRecentOpponents:
    return GameRecentOpponents(matches)


def _played(
    store: InMemoryMatchRecordRepository,
    one: object,
    other: object,
    *,
    at: datetime = NOW,
    status: MatchRecordStatus = MatchRecordStatus.ACTIVE,
) -> MatchRecord:
    """A match between two players, settled unless told otherwise."""
    record = MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(
            player_id=one,  # type: ignore[arg-type]
            queue_ticket_id=generate_uuid7(),
            accepted_at=at if status is MatchRecordStatus.ACTIVE else None,
        ),
        dark=MatchSeat(
            player_id=other,  # type: ignore[arg-type]
            queue_ticket_id=generate_uuid7(),
            accepted_at=at if status is MatchRecordStatus.ACTIVE else None,
        ),
        created_at=at,
        acceptance_deadline=at + WINDOW,
        status=status,
        settled_at=None if status.is_pending else at,
    )
    store.matches[record.id] = record
    return record


class TestThePortIsSatisfiedByGamePublic:
    """A64-015.3 predicted this implementation would drop in with no
    adapter. These two assertions are what make that checkable.

    Neither protocol is `@runtime_checkable`, deliberately — a structural
    `isinstance` matches on method *names* and would pass for a class whose
    signature is wrong. So the check is a **typed assignment**, which mypy
    verifies against the real signatures as part of the quality gate, with a
    runtime assertion that the object is the one that was assigned.
    """

    def test_the_reader_satisfies_game_s_published_port(
        self, opponents: GameRecentOpponents
    ) -> None:
        published: RecentOpponentReader = opponents

        assert published is opponents

    def test_it_also_satisfies_matchmaking_s_own_port(self, opponents: GameRecentOpponents) -> None:
        """The two protocols have the same shape deliberately, so the
        composition root wires one object with no adapter between them."""
        consumed: RecentOpponentProvider = opponents

        assert consumed is opponents


class TestTheReadItself:
    async def test_the_previous_opponent_is_reported(
        self, opponents: GameRecentOpponents, matches: InMemoryMatchRecordRepository
    ) -> None:
        one, other = generate_uuid7(), generate_uuid7()
        _played(matches, one, other)

        recent = await opponents.recent_opponents_among([one, other])

        assert recent[one] == frozenset({other})

    async def test_only_the_most_recent_match_counts(
        self, opponents: GameRecentOpponents, matches: InMemoryMatchRecordRepository
    ) -> None:
        """QT-3 excludes the *immediately previous* opponent, not everybody
        a player has ever met — an ever-growing exclusion set would empty a
        thin pool."""
        player, old, recent_one = generate_uuid7(), generate_uuid7(), generate_uuid7()
        _played(matches, player, old, at=NOW - timedelta(hours=1))
        _played(matches, player, recent_one, at=NOW)

        excluded = await opponents.recent_opponents_among([player, old, recent_one])

        assert excluded[player] == frozenset({recent_one})

    async def test_an_opponent_outside_the_batch_is_not_reported(
        self, opponents: GameRecentOpponents, matches: InMemoryMatchRecordRepository
    ) -> None:
        """The caller is deciding which of *these* tickets may meet, so an
        opponent who is not in the pool is a row nobody will compare
        against."""
        player, elsewhere = generate_uuid7(), generate_uuid7()
        also_queued = generate_uuid7()
        _played(matches, player, elsewhere)

        excluded = await opponents.recent_opponents_among([player, also_queued])

        assert excluded == {}

    async def test_a_pending_match_is_not_a_game_they_have_played(
        self, opponents: GameRecentOpponents, matches: InMemoryMatchRecordRepository
    ) -> None:
        """An offer nobody has answered is not history. Counting it would
        exclude a pair on the strength of a match that may be about to
        expire — and then they could not be re-paired either."""
        one, other = generate_uuid7(), generate_uuid7()
        _played(matches, one, other, status=MatchRecordStatus.PENDING_ACCEPTANCE)

        assert await opponents.recent_opponents_among([one, other]) == {}

    async def test_a_player_with_no_history_is_absent(self, opponents: GameRecentOpponents) -> None:
        one, other = generate_uuid7(), generate_uuid7()

        assert await opponents.recent_opponents_among([one, other]) == {}

    async def test_a_pool_of_one_asks_nothing(
        self, opponents: GameRecentOpponents, matches: InMemoryMatchRecordRepository
    ) -> None:
        """One candidate cannot be excluded from anybody, and a pool of one
        is the common shape of a quiet queue."""
        assert await opponents.recent_opponents_among([generate_uuid7()]) == {}

    async def test_an_unreadable_history_excludes_nobody(
        self, matches: InMemoryMatchRecordRepository
    ) -> None:
        """A rematch is a disappointment; a pairing scan that stops because
        a read failed is an empty queue, which is an outage."""

        class Broken:
            async def latest_opponent_among(self, player_ids: object) -> dict[object, object]:
                raise RuntimeError("the match table is unreachable")

        reader = GameRecentOpponents(Broken())  # type: ignore[arg-type]

        assert await reader.recent_opponents_among([generate_uuid7(), generate_uuid7()]) == {}


class TestThePreviousOpponentIsExcludedFromPairing:
    """The rule end to end: a real `PairingService` over a real
    `GameRecentOpponents`, with only storage substituted."""

    async def test_two_players_who_just_met_are_not_paired_again(
        self, opponents: GameRecentOpponents, matches: InMemoryMatchRecordRepository
    ) -> None:
        tickets = InMemoryQueueRepository()
        one = _queued(tickets)
        other = _queued(tickets)
        _played(matches, one.player_id, other.player_id)

        outcome = await _service(tickets, opponents).pair_once(pool=POOL)

        assert not outcome.paired
        assert tickets.tickets[one.id].status is QueueStatus.WAITING

    async def test_a_third_player_is_paired_instead(
        self, opponents: GameRecentOpponents, matches: InMemoryMatchRecordRepository
    ) -> None:
        """The exclusion vetoes a pair, not a player — an excluded pair must
        not take both of them out of the pool."""
        tickets = InMemoryQueueRepository()
        one = _queued(tickets)
        other = _queued(tickets)
        third = _queued(tickets)
        _played(matches, one.player_id, other.player_id)

        outcome = await _service(tickets, opponents).pair_once(pool=POOL)

        assert outcome.paired
        assert tickets.tickets[third.id].status is QueueStatus.MATCHED

    async def test_players_who_have_never_met_are_paired(
        self, opponents: GameRecentOpponents
    ) -> None:
        tickets = InMemoryQueueRepository()
        _queued(tickets)
        _queued(tickets)

        assert (await _service(tickets, opponents).pair_once(pool=POOL)).paired


def _queued(store: InMemoryQueueRepository) -> QueueTicket:
    ticket = QueueTicket(
        player_id=generate_uuid7(),
        pool=POOL,
        time_control=BLITZ,
        rating_snapshot=1500,
        entered_at=NOW,
        expires_at=NOW + TTL,
    )
    store.tickets[ticket.id] = ticket
    return ticket


def _service(tickets: InMemoryQueueRepository, opponents: GameRecentOpponents) -> PairingService:
    return PairingService(
        tickets=tickets,
        engine=PairingEngine(GENEROUS),
        exclusions=StubExclusions(),
        opponents=opponents,
        ratings=StubRatings(),
        matches=RecordingMatchCreation(),
        events=RecordingPublisher(),
        unit_of_work=NullUnitOfWork(),
        clock=MovableClock(NOW),
        metrics=NullMetrics(),
        candidate_batch_size=50,
        reservation_ttl_seconds=WINDOW.total_seconds(),
    )
