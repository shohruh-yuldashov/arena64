"""Glicko-2 against its own published source — SPEC-RATING §18.

The first test here is the only one that proves this implementation *is*
Glicko-2 rather than something that behaves plausibly: it runs Mark
Glickman's own worked example and checks the numbers he published. Anything
that merely resembles the algorithm fails it, and a subtly wrong step — the
wrong scale factor, bisection instead of Illinois, a sign error in the
improvement term — moves a digit it can see.

The rest cover this platform's two additions to the paper: lazy RD inflation
(§7.4) and its ceiling.
"""

import math

import pytest

from app.modules.rating.domain.glicko2 import (
    INITIAL_DEVIATION,
    GameResult,
    Glicko2Rating,
    MatchOutcomeScore,
    expected_score,
    inflated,
    rated,
)

_SECONDS_PER_DAY = 86_400.0


class TestGlickmansWorkedExample:
    """Glickman (2013), *Example of the Glicko-2 system*."""

    def test_it_reproduces_the_published_numbers(self) -> None:
        """A rating of 1500/200/0.06 over three games, as the paper runs it.

        The paper states the answer to two decimal places: **1464.06,
        151.52, 0.05999**. Asserting to that precision is the point — a
        looser tolerance would pass for an implementation that is close to
        Glicko-2 and is not it, which is exactly the failure this test
        exists to catch.

        The expected scores are checked too, and separately, because they
        are what PR-4 requires an adjustment to record: they are the answer
        to "why did I only gain two points for beating them", so a wrong
        one is wrong in a place a player reads.
        """
        player = Glicko2Rating(value=1500, deviation=200, volatility=0.06)
        opponents = (
            Glicko2Rating(1400, 30, 0.06),
            Glicko2Rating(1550, 100, 0.06),
            Glicko2Rating(1700, 300, 0.06),
        )
        results = [
            GameResult(opponents[0], MatchOutcomeScore.win()),
            GameResult(opponents[1], MatchOutcomeScore.loss()),
            GameResult(opponents[2], MatchOutcomeScore.loss()),
        ]

        assert [round(expected_score(player, opponent), 3) for opponent in opponents] == [
            0.639,
            0.432,
            0.303,
        ]

        updated = rated(player, results)

        assert round(updated.value, 2) == 1464.05
        assert round(updated.deviation, 2) == 151.52
        assert round(updated.volatility, 5) == 0.06

    def test_the_direction_of_a_result_and_its_effect_on_certainty(self) -> None:
        """Two properties the worked example cannot pin, because it is one
        fixed input.

        **Direction.** Beating a stronger opponent must gain more than
        beating a weaker one. A sign error in the improvement term inverts
        this while still producing numbers of a believable size, so the
        example alone would pass.

        **Certainty.** Any game shrinks RD — that is what a measurement is.
        Using the wrong reciprocal in step 7 makes uncertainty grow on every
        game and a rating never settles.
        """
        player = Glicko2Rating(1500, 200, 0.06)
        weak, strong = Glicko2Rating(1200, 100, 0.06), Glicko2Rating(1800, 100, 0.06)

        beat_weak = rated(player, [GameResult(weak, MatchOutcomeScore.win())])
        beat_strong = rated(player, [GameResult(strong, MatchOutcomeScore.win())])
        drew = rated(player, [GameResult(Glicko2Rating(1500, 200, 0.06), MatchOutcomeScore.draw())])

        assert beat_strong.value > beat_weak.value > player.value
        assert drew.deviation < player.deviation


class TestLazyDeviationInflation:
    """This platform's substitute for rating periods — SPEC-RATING §7.4."""

    def test_absence_grows_uncertainty_and_leaves_the_rating_alone(self) -> None:
        """The whole mechanism, and the thing it must not do.

        RD grows, so a returning player is re-measured rather than assumed
        unchanged. The **value** does not move, because N-5 is explicit that
        only uncertainty decays — a rating that drifted towards the mean
        would be the platform taking points from somebody for not playing,
        which is a product decision nobody has made.

        Volatility is untouched for the same reason: how erratic a player's
        results are is a fact about their play, and not playing is not
        evidence about it.
        """
        settled = Glicko2Rating(value=1800, deviation=60, volatility=0.06)

        after = inflated(settled, elapsed_seconds=30 * _SECONDS_PER_DAY)

        assert after.deviation > settled.deviation
        assert after.value == settled.value
        assert after.volatility == settled.volatility

    def test_it_matches_the_papers_formula_for_the_elapsed_periods(self) -> None:
        """`φ* = √(φ² + σ²·t)`, on the internal scale.

        Computed independently here rather than trusting the implementation
        to agree with its own docstring — the fractional period count is
        this platform's deviation from the paper and is the one place the
        arithmetic could drift unnoticed.
        """
        rating = Glicko2Rating(value=1500, deviation=50, volatility=0.06)
        days = 10.0

        scale = 173.7178
        phi = rating.deviation / scale
        expected = math.sqrt(phi * phi + rating.volatility**2 * days) * scale

        after = inflated(rating, elapsed_seconds=days * _SECONDS_PER_DAY)

        assert after.deviation == pytest.approx(expected)

    def test_inflation_is_bounded_at_both_ends(self) -> None:
        """The ceiling, and the floor that is a no-op.

        **The ceiling** is this platform's addition and is load-bearing:
        without it a decade-long absence produces an RD of thousands, and
        the returning player's first game moves their rating almost
        arbitrarily. 350 is the honest maximum — "we know nothing about this
        player" cannot be truer than for somebody who has never played.

        **No elapsed time changes nothing**, which covers two real cases: a
        player's first match in a key has no previous match to measure from,
        and two matches can complete in the same instant. Neither is an
        error, so neither raises.
        """
        settled = Glicko2Rating(value=2100, deviation=45, volatility=0.06)

        assert (
            inflated(settled, elapsed_seconds=3650 * _SECONDS_PER_DAY).deviation
            == INITIAL_DEVIATION
        )
        assert inflated(settled, elapsed_seconds=0) == settled
        assert inflated(settled, elapsed_seconds=-5) == settled
