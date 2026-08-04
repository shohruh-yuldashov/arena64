"""The rating aggregate — SPEC-RATING §7.5, §7.7.

`test_glicko2.py` proves the arithmetic against Glickman's published
example. This proves what the aggregate adds on top of it: the provisional
boundary, lazy inflation being applied *inside* the update rather than by a
caller who might forget, the adjustment that PR-4 requires, and the frozen
refusal PR-5 specifies.

Three tests, because §18 caps the phase and these are the three things that
would be wrong in a way A-4 forbids — a rating that cannot be explained, one
that was computed from the wrong inputs, or one that moved when it was held.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.game.public import ProductVariant
from app.modules.rating.domain.glicko2 import Glicko2Rating, MatchOutcomeScore
from app.modules.rating.domain.keys import RatingKey, SpeedClass
from app.modules.rating.domain.player_rating import (
    ALGORITHM_VERSION,
    PROVISIONAL_GAMES_THRESHOLD,
    PlayerRating,
    RatingFrozen,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
KEY = RatingKey(variant=ProductVariant.RUSSIAN_8X8, speed_class=SpeedClass.CLASSICAL)


def _rating(**overrides: object) -> PlayerRating:
    base = {"player_id": uuid4(), "key": KEY, "rating": Glicko2Rating.initial()}
    return PlayerRating(**{**base, **overrides})  # type: ignore[arg-type]


class TestApplyingAMatch:
    def test_it_returns_a_new_rating_and_an_adjustment_that_explains_it(self) -> None:
        """PR-4, and the reason both values come back from one call.

        The adjustment must be reconstructible into the transition without
        consulting anything else: `before`, `after`, the opponent's whole
        triple, the expectation the arithmetic actually used, and the
        algorithm version that produced it. A caller handed only the new
        rating would have to assemble that from whatever it happened to be
        holding, and the first one to get it wrong leaves a hole in the one
        dataset A-4 says must reconcile exactly.

        `points_gained` is derived rather than stored — it is the number a
        player quotes back at support, and a stored copy is a second number
        that can disagree with the two it comes from.
        """
        player = _rating()
        opponent = Glicko2Rating(1400, 30, 0.06)
        match_id = uuid4()

        updated, adjustment = player.applied(
            opponent=opponent, score=MatchOutcomeScore.win(), match_id=match_id, at=NOW
        )

        assert updated.rating.value > player.rating.value
        assert updated.games_played == 1
        assert updated.last_rated_at == NOW
        assert updated.peak_value == updated.rating.value
        assert updated.peak_at == NOW

        assert adjustment.match_id == match_id
        assert adjustment.before == player.rating
        assert adjustment.after == updated.rating
        assert adjustment.opponent == opponent
        assert adjustment.actual_score == 1.0
        assert 0.0 < adjustment.expected_score < 1.0
        assert adjustment.algorithm_version == ALGORITHM_VERSION
        assert adjustment.points_gained == pytest.approx(updated.rating.value - player.rating.value)
        # §12 — the column exists so a season introduced later can be
        # written onto adjustments made after it, never onto these.
        assert adjustment.season_id is None

    def test_absence_is_inflated_inside_the_update_from_the_last_rated_instant(
        self,
    ) -> None:
        """§7.4, and the reason it is not the caller's job.

        Two identical players meet identical opponents. One played
        yesterday; the other has not played for a year. The absent player's
        update starts from a **larger deviation**, so the same result moves
        their rating further — which is the whole point of measuring
        uncertainty at all.

        The assertion is on `adjustment.before`, not on the returned rating,
        because that is where inflation is observable: it is the value the
        arithmetic ran on, and a caller that had to inflate first would
        produce an adjustment whose `before` was the stale stored triple.

        A player whose first rated match this is has no absence to measure.
        Asserted here too, because inflating from the epoch would hand every
        new player the deviation ceiling.
        """
        settled = Glicko2Rating(1600, 60, 0.06)
        opponent = Glicko2Rating(1600, 60, 0.06)

        recent = _rating(rating=settled, games_played=40, last_rated_at=NOW - timedelta(days=1))
        absent = _rating(rating=settled, games_played=40, last_rated_at=NOW - timedelta(days=365))
        newcomer = _rating(rating=settled, games_played=0)

        _, recent_adjustment = recent.applied(
            opponent=opponent, score=MatchOutcomeScore.win(), match_id=uuid4(), at=NOW
        )
        _, absent_adjustment = absent.applied(
            opponent=opponent, score=MatchOutcomeScore.win(), match_id=uuid4(), at=NOW
        )
        _, first_adjustment = newcomer.applied(
            opponent=opponent, score=MatchOutcomeScore.win(), match_id=uuid4(), at=NOW
        )

        assert absent_adjustment.before.deviation > recent_adjustment.before.deviation
        assert absent_adjustment.points_gained > recent_adjustment.points_gained
        # No previous match, so nothing to inflate from.
        assert first_adjustment.before == settled

    def test_a_frozen_rating_refuses_rather_than_quietly_doing_nothing(self) -> None:
        """PR-5, and why the refusal is an exception.

        Returning the rating unchanged would make a fair-play hold
        indistinguishable from a match that happened to move nothing, and
        §17 requires the refusal to be counted.

        Unreachable in v0.5.0 — nothing sets the flag, because `fairplay`
        does not exist — so this test is what keeps the extension point
        working until something does.
        """
        held = _rating(games_played=40).frozen()

        with pytest.raises(RatingFrozen):
            held.applied(
                opponent=Glicko2Rating.initial(),
                score=MatchOutcomeScore.draw(),
                match_id=uuid4(),
                at=NOW,
            )


class TestProvisionalStatus:
    def test_it_ends_at_exactly_the_threshold_and_is_never_stored(self) -> None:
        """PR-6's mark, at the boundary — SPEC-RATING §7.5.

        The boundary is where an off-by-one lives, so it is asserted on both
        sides of 25 rather than at a comfortable distance from it. A player
        with 24 rated games is provisional; one with 25 is not.

        Derived rather than stored, and that is the property being fixed: a
        stored flag is a second copy of what `games_played` already says,
        and the copy is what goes stale.
        """
        assert PlayerRating.unrated(uuid4(), KEY).is_provisional
        assert _rating(games_played=PROVISIONAL_GAMES_THRESHOLD - 1).is_provisional
        assert not _rating(games_played=PROVISIONAL_GAMES_THRESHOLD).is_provisional

        # The transition happens through an ordinary update — nothing sets
        # a flag, so nothing can forget to.
        last_provisional = _rating(games_played=PROVISIONAL_GAMES_THRESHOLD - 1, last_rated_at=NOW)
        established, _ = last_provisional.applied(
            opponent=Glicko2Rating.initial(),
            score=MatchOutcomeScore.draw(),
            match_id=uuid4(),
            at=NOW,
        )
        assert not established.is_provisional
