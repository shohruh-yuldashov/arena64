"""Closing a match that will never be played — A64-025.13A §36.

The port `TournamentNoShowService` was missing. `tests/contract/
test_tournament_matches.py` proves the sweep uses it against a real
database; these prove the port's own contract, which is the half a caller
depends on and cannot see: that it is idempotent, that it refuses a game
that was played, and that it does not invent a result.

## Why "does not invent a result" is asserted rather than assumed

These fixtures are **rated**. An abort that recorded `WIN` would move two
Glicko-2 ratings for a game nobody played and put it in both players'
history — which is what A64-019.5H was protecting against when it left the
match `active` instead. The protection was right; the mechanism was not.
`MatchOutcome.NONE` is what makes both true at once, and MT-11 is the rule
that keeps it out of every rating and statistic.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION, PlayerSide
from app.modules.game.application.services.match_abort_service import PersistentMatchAbort
from app.modules.game.domain.events import MatchCompleted
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.domain.result import (
    MatchOutcome,
    MatchResult,
    TerminationReason,
)
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.public.abort import AbortMatchRequest, AbortOutcome
from tests.fakes.matches import InMemoryMatchRecordRepository
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import RecordingPublisher

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _active_match() -> MatchRecord:
    """A tournament fixture: activated by the system, never accepted, no clock."""
    created = MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(player_id=generate_uuid7(), queue_ticket_id=generate_uuid7()),
        dark=MatchSeat(player_id=generate_uuid7(), queue_ticket_id=generate_uuid7()),
        created_at=NOW,
        acceptance_deadline=NOW + timedelta(seconds=30),
    )
    return created.system_activated(NOW)


@pytest.fixture
def matches() -> InMemoryMatchRecordRepository:
    return InMemoryMatchRecordRepository()


@pytest.fixture
def events() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def service(
    matches: InMemoryMatchRecordRepository, events: RecordingPublisher
) -> PersistentMatchAbort:
    return PersistentMatchAbort(
        matches=matches,
        events=events,
        unit_of_work=NullUnitOfWork(),
        clock=MovableClock(NOW),
    )


class TestClosingAnUnplayedMatch:
    async def test_it_ends_the_match_without_inventing_a_result(
        self,
        service: PersistentMatchAbort,
        matches: InMemoryMatchRecordRepository,
    ) -> None:
        """The whole design, in one assertion pair.

        Completed, so the player is no longer carrying a live match and the
        lobby stops sending them to a game room. `NONE`, so nothing claims
        that anybody won.
        """
        record, _ = await matches.create(_active_match())

        outcome = await service.abort(AbortMatchRequest(match_id=record.id))

        assert outcome is AbortOutcome.ABORTED
        stored = await matches.by_id(record.id)
        assert stored is not None
        assert stored.status is MatchRecordStatus.COMPLETED
        assert stored.result is not None
        assert stored.result.outcome is MatchOutcome.NONE
        assert stored.result.reason is TerminationReason.ABORT
        assert stored.result.winner is None

    async def test_it_publishes_the_completion_so_consumers_settle(
        self,
        service: PersistentMatchAbort,
        matches: InMemoryMatchRecordRepository,
        events: RecordingPublisher,
    ) -> None:
        """Without the event the tournament's reconciler keeps re-reading a
        match it believes unfinished, and the gateway leaves a room open on
        a game that has ended.

        `origin` and `origin_ref` travel with it for the reason the clock
        adjudicator records: the originating context has to recognise its
        own fixture in the completion.
        """
        record, _ = await matches.create(_active_match())

        await service.abort(AbortMatchRequest(match_id=record.id))

        published = [e for e in events.published if isinstance(e, MatchCompleted)]
        assert len(published) == 1
        assert published[0].match_id == record.id
        assert published[0].outcome is MatchOutcome.NONE
        assert published[0].winner is None

    async def test_a_second_call_writes_nothing(
        self,
        service: PersistentMatchAbort,
        matches: InMemoryMatchRecordRepository,
        events: RecordingPublisher,
    ) -> None:
        """It has to be idempotent: the no-show sweep re-claims an attempt
        whose worker died, so this is called again for a match it closed."""
        record, _ = await matches.create(_active_match())

        first = await service.abort(AbortMatchRequest(match_id=record.id))
        second = await service.abort(AbortMatchRequest(match_id=record.id))

        assert (first, second) == (AbortOutcome.ABORTED, AbortOutcome.ALREADY_SETTLED)
        assert len([e for e in events.published if isinstance(e, MatchCompleted)]) == 1

    async def test_a_played_game_beats_a_stale_sweep(
        self,
        service: PersistentMatchAbort,
        matches: InMemoryMatchRecordRepository,
    ) -> None:
        """A real result that arrived while the caller held its claim wins.

        The same rule the sweep applies to a superseded attempt, enforced on
        this side of the boundary as well — so a caller that forgot it
        cannot close a game that was played to an end.
        """
        record, _ = await matches.create(_active_match())
        played = record.completed(
            MatchResult(
                outcome=MatchOutcome.WIN,
                reason=TerminationReason.RESIGNATION,
                winner=PlayerSide.LIGHT,
            ),
            ply_number=record.ply_number,
            at=NOW,
        )
        assert await matches.advance(played, expected_ply=record.ply_number)

        outcome = await service.abort(AbortMatchRequest(match_id=record.id))

        assert outcome is AbortOutcome.ALREADY_SETTLED
        stored = await matches.by_id(record.id)
        assert stored is not None
        assert stored.result is not None
        assert stored.result.reason is TerminationReason.RESIGNATION

    async def test_an_unknown_match_is_reported_rather_than_raised(
        self, service: PersistentMatchAbort
    ) -> None:
        """A sweep must not stop. A caller holding a dangling id is a defect
        to log and carry on from, not an exception to propagate through a
        scheduled pass."""
        unknown: UUID = generate_uuid7()

        assert await service.abort(AbortMatchRequest(match_id=unknown)) is AbortOutcome.NOT_FOUND
