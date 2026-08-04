"""Applying a completed match to two ratings — SPEC-RATING §9, §11.

The arithmetic is `test_glicko2.py`'s and is not repeated here. What this
covers is everything the *service* decides: which completions rate, that
both players move together or not at all, that a redelivery is a no-op, and
that the numbers come from the seat snapshots rather than from whatever the
players rate now.

The last is the one that cannot be caught by inspection. A service that read
current ratings passes every single-match test ever written; it fails only
when two matches complete at once, or — as here — when the stored rating has
deliberately been made to differ from the snapshot.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.game.public import ProductVariant, TerminationReason
from app.modules.rating.application.ports import PlayerRatingRepository
from app.modules.rating.application.services.match_rating_service import (
    CompletedMatch,
    CompletedSeat,
    MatchRatingOutcome,
    MatchRatingService,
)
from app.modules.rating.domain.glicko2 import Glicko2Rating
from app.modules.rating.domain.keys import RatingKey, SpeedClass
from app.modules.rating.domain.player_rating import PlayerRating
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import RecordingPublisher
from tests.fakes.rating import InMemoryPlayerRatings

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
KEY = RatingKey(variant=ProductVariant.RUSSIAN_8X8, speed_class=SpeedClass.CLASSICAL)


def _seat(player_id=None, *, value: float = 1500.0) -> CompletedSeat:  # type: ignore[no-untyped-def]
    return CompletedSeat(
        player_id=player_id or uuid4(), value=value, deviation=200.0, volatility=0.06
    )


def _completion(
    *,
    light: CompletedSeat | None = None,
    dark: CompletedSeat | None = None,
    winner: str | None = "light",
    rated: bool = True,
    termination: TerminationReason = TerminationReason.NO_LEGAL_MOVES,
) -> CompletedMatch:
    return CompletedMatch(
        match_id=uuid4(),
        key=KEY,
        rated=rated,
        termination=termination,
        winner=winner,
        light=light or _seat(),
        dark=dark or _seat(),
    )


def _service(ratings: PlayerRatingRepository, events: RecordingPublisher) -> MatchRatingService:
    return MatchRatingService(
        ratings=ratings, events=events, unit_of_work=NullUnitOfWork(), clock=MovableClock(NOW)
    )


class TestWhichCompletionsRate:
    @pytest.mark.asyncio
    async def test_a_decisive_result_moves_both_players_in_opposite_directions(
        self,
    ) -> None:
        """The ordinary case, and the invariant underneath it.

        The winner gains and the loser loses — but the assertion that
        matters is that **both** moved. A service that updated one and
        returned would pass a test asserting only the winner's gain, and it
        would leave a ladder that no longer sums, permanently (A-4).

        Two `rating.updated` events, one per player, because a leaderboard
        consumer acts on one player at a time.
        """
        ratings, events = InMemoryPlayerRatings(), RecordingPublisher()
        completion = _completion()

        outcome = await _service(ratings, events).apply(completion)

        assert outcome == MatchRatingOutcome.APPLIED
        winner = ratings.stored[(completion.light.player_id, KEY)]
        loser = ratings.stored[(completion.dark.player_id, KEY)]
        assert winner.rating.value > 1500.0 > loser.rating.value
        assert winner.games_played == loser.games_played == 1
        assert len(events.published) == 2

    @pytest.mark.asyncio
    async def test_a_draw_moves_both_and_a_timeout_is_rated(self) -> None:
        """§9's two cases that a naive allowlist gets wrong.

        A **draw** still moves both ratings — towards each other, by the
        amount their difference did not predict — so "no winner" must not be
        read as "no update".

        A **timeout** is rated, including when the player who lost had
        disconnected. Disconnection is not a termination; the clock is, and
        it ran out. Treating it otherwise would make disconnecting a way to
        avoid a loss.
        """
        ratings, events = InMemoryPlayerRatings(), RecordingPublisher()
        drawn = _completion(
            light=_seat(value=1700.0),
            dark=_seat(value=1300.0),
            winner=None,
            termination=TerminationReason.AGREED_DRAW,
        )

        assert await _service(ratings, events).apply(drawn) == MatchRatingOutcome.APPLIED
        # The stronger player drew and lost points; the weaker gained.
        assert ratings.stored[(drawn.light.player_id, KEY)].rating.value < 1700.0
        assert ratings.stored[(drawn.dark.player_id, KEY)].rating.value > 1300.0

        flagged = _completion(termination=TerminationReason.FLAG)
        assert await _service(ratings, events).apply(flagged) == MatchRatingOutcome.APPLIED

    @pytest.mark.asyncio
    async def test_casual_aborted_and_administrative_completions_are_ignored(self) -> None:
        """§9's exclusions, asserted by their *absence* from the store.

        Nothing is written and nothing is published — checked rather than
        the return value alone, because a service that stored a zero-delta
        adjustment would also return `not_rateable` while quietly filling a
        permanent table with rows for games that never counted.
        """
        ratings, events = InMemoryPlayerRatings(), RecordingPublisher()
        service = _service(ratings, events)

        for completion in (
            _completion(rated=False),
            _completion(termination=TerminationReason.ABORT),
            _completion(termination=TerminationReason.ADJUDICATION),
            _completion(termination=TerminationReason.ABANDONMENT),
        ):
            assert await service.apply(completion) == MatchRatingOutcome.NOT_RATEABLE

        assert ratings.stored == {}
        assert events.published == []


class TestTheInputsAreTheSeatSnapshots:
    @pytest.mark.asyncio
    async def test_the_snapshot_is_used_even_when_the_stored_rating_has_moved_on(
        self,
    ) -> None:
        """PR-3, and the only way to catch a service that ignores it.

        Both players have **stored** ratings far from what they rated when
        this match was created. A service that read the current value would
        compute a large correction; one that uses the snapshot computes the
        result of the game that was actually played.

        The adjustment's `before` is asserted rather than the final rating,
        because that is where the input is visible — and it is what a player
        disputing a change would be shown.

        This is what makes two matches completing at once safe: neither
        sees the other's partial result, because neither reads a current
        rating at all.
        """
        ratings, events = InMemoryPlayerRatings(), RecordingPublisher()
        light_id, dark_id = uuid4(), uuid4()

        # What they rate *now* — nothing may use these numbers.
        ratings.stored[(light_id, KEY)] = PlayerRating(
            player_id=light_id,
            key=KEY,
            rating=Glicko2Rating(2400, 40, 0.06),
            games_played=300,
            last_rated_at=NOW,
        )
        ratings.stored[(dark_id, KEY)] = PlayerRating(
            player_id=dark_id,
            key=KEY,
            rating=Glicko2Rating(900, 40, 0.06),
            games_played=300,
            last_rated_at=NOW,
        )

        # What they rated when they sat down.
        completion = _completion(
            light=_seat(light_id, value=1500.0), dark=_seat(dark_id, value=1500.0)
        )
        await _service(ratings, events).apply(completion)

        assert [adjustment.before.value for adjustment in ratings.adjustments] == [1500.0, 1500.0]
        assert [adjustment.opponent.value for adjustment in ratings.adjustments] == [
            1500.0,
            1500.0,
        ]


class TestExactlyOnceAndAtomicity:
    @pytest.mark.asyncio
    async def test_a_redelivered_completion_does_not_apply_twice(self) -> None:
        """PR-1, at the service. The constraint itself is
        `tests/contract/test_rating_persistence.py`'s.

        The second call is refused by the repository's unique constraint and
        reported as `already_applied` — a **success**, because the work was
        done by whoever won the race. Both players' counters are asserted
        afterwards, because the failure this guards against is not "an error
        was raised" but "the rating moved twice".
        """
        ratings, events = InMemoryPlayerRatings(), RecordingPublisher()
        service, completion = _service(ratings, events), _completion()

        assert await service.apply(completion) == MatchRatingOutcome.APPLIED
        assert await service.apply(completion) == MatchRatingOutcome.ALREADY_APPLIED

        assert ratings.stored[(completion.light.player_id, KEY)].games_played == 1
        assert ratings.stored[(completion.dark.player_id, KEY)].games_played == 1
        assert len(events.published) == 2

    @pytest.mark.asyncio
    async def test_a_failure_on_the_second_player_leaves_the_first_untouched(self) -> None:
        """§4 — a partial update must be impossible.

        The store is made to fail on the *second* save, which is the only
        ordering that can produce a half-rated match. The transaction rolls
        back, so neither player moved and nothing was published.

        Asserted through the unit of work rather than by inspecting the
        store, because "the write was rolled back" is the guarantee — a fake
        that discarded the first write on its own would prove nothing about
        the real transaction.
        """
        ratings, events = InMemoryPlayerRatings(fail_on_save=2), RecordingPublisher()
        unit_of_work = NullUnitOfWork()
        service = MatchRatingService(
            ratings=ratings, events=events, unit_of_work=unit_of_work, clock=MovableClock(NOW)
        )

        with pytest.raises(RuntimeError):
            await service.apply(_completion())

        assert unit_of_work.commits == 0
        assert events.published == []

    @pytest.mark.asyncio
    async def test_a_frozen_rating_stops_the_whole_match_not_half_of_it(self) -> None:
        """PR-5, and §6's "do not partially update the other player".

        Only `dark` is frozen, and `light` is checked first — so a service
        that refused lazily, at the moment it reached the frozen aggregate,
        would already have written `light`. Both are checked before either
        write, which makes the refusal a property of the match rather than
        of which seat came first.

        The whole match is refused and **nothing is queued**: SPEC-RATING
        §13 records that the adjustment is lost rather than deferred, which
        is the documented limitation until `fairplay` exists.
        """
        ratings, events = InMemoryPlayerRatings(), RecordingPublisher()
        completion = _completion()
        ratings.stored[(completion.dark.player_id, KEY)] = PlayerRating(
            player_id=completion.dark.player_id, key=KEY, rating=Glicko2Rating.initial()
        ).frozen()

        assert await _service(ratings, events).apply(completion) == MatchRatingOutcome.FROZEN

        assert (completion.light.player_id, KEY) not in ratings.stored
        assert ratings.adjustments == []
        assert events.published == []
