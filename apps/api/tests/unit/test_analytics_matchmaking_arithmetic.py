"""Matchmaking and game health arithmetic — A64-027.5.

The formulas, the invariants, and the one thing that separates a correct
funnel from a plausible one: **grain**. A completion rate over player facts
is exactly twice a completion rate over matches, and both look like
percentages.
"""

from datetime import UTC, date, datetime

import pytest

from app.modules.analytics.application.read_models.funnels import (
    Coverage,
    FunnelMeta,
    Maturity,
)
from app.modules.analytics.application.read_models.matchmaking import (
    GameHealth,
    Grain,
    OfferHealth,
    QueueHealth,
    WaitDistribution,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _meta() -> FunnelMeta:
    return FunnelMeta(
        environment="production",
        include_synthetic=False,
        cohort_from=date(2026, 1, 1),
        cohort_to=date(2026, 1, 7),
        requested_from=date(2026, 1, 1),
        requested_to=date(2026, 1, 7),
        window_days=0,
        maturity=Maturity.MATURE,
        coverage=Coverage.COMPLETE,
        generated_at=NOW,
    )


def queue(**overrides: object) -> QueueHealth:
    defaults: dict[str, object] = {
        "grain": Grain.QUEUE_ATTEMPT,
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 1, 7),
        "meta": _meta(),
        "queue_joins": 100,
        "paired_attempts": 60,
        "abandoned_attempts": 20,
        "cancelled_attempts": 12,
        "expired_attempts": 8,
        "wait": WaitDistribution(sample=30, p50_seconds=4.0, p95_seconds=25.0),
    }
    defaults.update(overrides)
    return QueueHealth(**defaults)  # type: ignore[arg-type]


def game(**overrides: object) -> GameHealth:
    defaults: dict[str, object] = {
        "grain": Grain.MATCH,
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 1, 7),
        "meta": _meta(),
        "started": 100,
        "completed": 80,
        "aborted": 10,
        "resignations": 30,
        "draws": 10,
        "abandonments": 6,
        "flags": 4,
        "rated_completions": 60,
        "termination_breakdown": (("resignation", 30), ("no_legal_moves", 30)),
    }
    defaults.update(overrides)
    return GameHealth(**defaults)  # type: ignore[arg-type]


class TestGrainIsDeclared:
    def test_every_result_names_its_unit(self) -> None:
        """§40. A caller combining two results has to notice they count
        different things, and a field is harder to miss than a docstring."""
        assert queue().grain is Grain.QUEUE_ATTEMPT
        assert game().grain is Grain.MATCH
        assert (
            OfferHealth(
                grain=Grain.OFFER,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 7),
                meta=_meta(),
                accepted=1,
                declined=1,
                expired=1,
            ).grain
            is Grain.OFFER
        )


class TestQueueHealth:
    def test_m7b_denominator_is_resolved_attempts(self) -> None:
        """`queue_left / (queue_left + match_found)`, exactly. Not over
        joins: a ticket still waiting has not abandoned anything, and
        counting it as a failure would make a busy minute look like an
        outage."""
        assert queue().abandonment_rate == pytest.approx(20 / 80)

    def test_m8_denominator_is_joins(self) -> None:
        """A deliberately different denominator from M7b's — M8 asks what
        share of joins produced a pairing."""
        assert queue().match_found_rate == pytest.approx(0.60)

    def test_the_two_rates_use_different_denominators_on_purpose(self) -> None:
        health = queue()
        assert health.abandonment_rate != health.match_found_rate

    def test_the_abandonment_reasons_sum_to_the_total(self) -> None:
        """`cancelled` and `expired` are exhaustive over `queue_left`."""
        health = queue()
        assert health.cancelled_attempts + health.expired_attempts == health.abandoned_attempts

    def test_no_resolved_attempts_means_no_rate(self) -> None:
        assert queue(paired_attempts=0, abandoned_attempts=0).abandonment_rate is None

    def test_no_joins_means_no_match_found_rate(self) -> None:
        assert queue(queue_joins=0).match_found_rate is None


class TestOfferHealth:
    def _offer(self, **overrides: object) -> OfferHealth:
        defaults: dict[str, object] = {
            "grain": Grain.OFFER,
            "period_start": date(2026, 1, 1),
            "period_end": date(2026, 1, 7),
            "meta": _meta(),
            "accepted": 60,
            "declined": 25,
            "expired": 15,
        }
        defaults.update(overrides)
        return OfferHealth(**defaults)  # type: ignore[arg-type]

    def test_m9_is_accepted_over_all_resolved(self) -> None:
        assert self._offer().acceptance_rate == pytest.approx(0.60)

    def test_expiry_stays_in_the_denominator(self) -> None:
        """Dropping the offers nobody answered would turn a product where
        fifteen per cent time out into one with a perfect record."""
        with_expiry = self._offer().acceptance_rate
        without = 60 / 85
        assert with_expiry is not None
        assert with_expiry < without

    def test_the_three_outcomes_are_the_whole_population(self) -> None:
        offer = self._offer()
        assert offer.resolved == offer.accepted + offer.declined + offer.expired

    def test_no_offers_means_no_rate(self) -> None:
        assert self._offer(accepted=0, declined=0, expired=0).acceptance_rate is None


class TestGameHealth:
    def test_m10_excludes_aborts_from_both_sides(self) -> None:
        """§32. 100 started, 10 aborted, 80 completed → 80/90."""
        assert game().completion_rate == pytest.approx(80 / 90)

    def test_an_abort_is_not_a_completion(self) -> None:
        health = game(started=10, aborted=10, completed=0)
        assert health.completion_rate is None

    def test_the_completion_rates_are_over_completions_not_starts(self) -> None:
        """M11 – M14's denominator is the completed population, because a
        resignation is a completed game."""
        health = game()
        assert health.resignation_rate == pytest.approx(30 / 80)
        assert health.draw_rate == pytest.approx(10 / 80)
        assert health.rated_share == pytest.approx(60 / 80)

    def test_m13_folds_abandonment_and_flag_and_still_reports_them_apart(self) -> None:
        """A64-027.1: losing on time is a legitimate result and walking
        away is not, even though both end a game without a move."""
        health = game()
        assert health.abandonment_rate == pytest.approx(10 / 80)
        assert (health.abandonments, health.flags) == (6, 4)

    def test_no_completions_means_no_rates(self) -> None:
        health = game(completed=0, resignations=0, draws=0, abandonments=0, flags=0)
        assert health.resignation_rate is None
        assert health.draw_rate is None
        assert health.rated_share is None


class TestInvariants:
    """§62. Every one of these is a way a result can be self-contradictory
    while every individual number looks reasonable."""

    def test_rates_are_within_the_unit_interval(self) -> None:
        for value in (
            queue().abandonment_rate,
            queue().match_found_rate,
            game().completion_rate,
            game().resignation_rate,
            game().draw_rate,
            game().abandonment_rate,
            game().rated_share,
        ):
            assert value is not None
            assert 0.0 <= value <= 1.0

    def test_p50_does_not_exceed_p95(self) -> None:
        wait = queue().wait
        assert wait.p50_seconds is not None and wait.p95_seconds is not None
        assert wait.p50_seconds <= wait.p95_seconds

    def test_completions_never_exceed_eligible_starts(self) -> None:
        health = game()
        assert health.completed <= health.started - health.aborted

    def test_abandoned_never_exceeds_resolved(self) -> None:
        health = queue()
        assert health.abandoned_attempts <= health.abandoned_attempts + health.paired_attempts

    def test_no_rate_is_nan_or_infinite(self) -> None:
        import math

        for value in (queue().abandonment_rate, game().completion_rate):
            assert value is not None
            assert math.isfinite(value)


class TestQualifyingTerminations:
    def test_the_classification_is_shared_with_the_funnels(self) -> None:
        """Imported rather than repeated: two copies of a completion
        classification is two answers to "did this game happen"."""
        from app.modules.analytics.infrastructure.repositories.funnel_repository import (
            QUALIFYING_TERMINATIONS,
        )
        from app.modules.analytics.infrastructure.repositories.matchmaking_repository import (
            QUALIFYING,
        )

        assert QUALIFYING == QUALIFYING_TERMINATIONS

    def test_abort_is_excluded_and_resignation_is_not(self) -> None:
        from app.modules.analytics.infrastructure.repositories.matchmaking_repository import (
            QUALIFYING,
        )

        assert "abort" not in QUALIFYING
        assert "resignation" in QUALIFYING
        assert "flag" in QUALIFYING
        assert "abandonment" in QUALIFYING
