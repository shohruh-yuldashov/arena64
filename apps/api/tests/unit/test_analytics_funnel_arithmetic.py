"""Funnel arithmetic — A64-027.3 §30, §31, §53, §54.

The SQL counts people; this turns counts into a funnel. Separating them is
what makes the subtle half testable without a database, and the subtle half
is where the mistakes are: a denominator of nought, a drop-off measured
against the wrong stage, a rate that is sometimes a percentage.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.modules.analytics.application.read_models.funnels import (
    Coverage,
    DataQuality,
    DurationSummary,
    FunnelMeta,
    FunnelResult,
    Maturity,
)
from app.modules.analytics.application.services.funnels import (
    ACTIVATION_STAGES,
    build_funnel,
    rate,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _meta() -> FunnelMeta:
    return FunnelMeta(
        environment="production",
        include_synthetic=False,
        cohort_from=date(2026, 1, 1),
        cohort_to=date(2026, 1, 31),
        requested_from=date(2026, 1, 1),
        requested_to=date(2026, 1, 31),
        window_days=365,
        maturity=Maturity.MATURE,
        coverage=Coverage.COMPLETE,
        generated_at=NOW,
    )


class TestRate:
    def test_it_is_a_fraction_not_a_percentage(self) -> None:
        """One convention, and the one that composes: a rate of a rate is
        still a rate."""
        assert rate(1, 4) == 0.25

    def test_an_empty_denominator_has_no_answer(self) -> None:
        """§31. Nought out of nought is not zero per cent — a dashboard
        printing 0% would show a failure that did not happen."""
        assert rate(0, 0) is None
        assert rate(5, 0) is None

    def test_a_negative_denominator_has_no_answer_either(self) -> None:
        assert rate(1, -1) is None


class TestTheFunnelShape:
    """§50's worked example, with the numbers the task names."""

    COUNTS = {
        "user_registered": 100,
        "email_verified": 80,
        "queue_joined": 60,
        "match_started": 50,
        "activated": 40,
    }

    def test_the_stages_are_in_the_declared_order(self) -> None:
        """A funnel whose stages come back in dictionary order is one whose
        conversions are computed against whichever stage sorted first."""
        stages = build_funnel(ACTIVATION_STAGES, self.COUNTS)
        assert [stage.stage for stage in stages] == list(ACTIVATION_STAGES)

    def test_the_first_stage_has_no_predecessor(self) -> None:
        first = build_funnel(ACTIVATION_STAGES, self.COUNTS)[0]
        assert first.conversion_from_previous is None
        assert first.drop_off == 0
        assert first.drop_off_rate is None
        assert first.conversion_from_start == 1.0

    def test_conversion_from_previous(self) -> None:
        stages = build_funnel(ACTIVATION_STAGES, self.COUNTS)
        assert stages[1].conversion_from_previous == pytest.approx(0.80)
        assert stages[2].conversion_from_previous == pytest.approx(0.75)
        assert stages[3].conversion_from_previous == pytest.approx(50 / 60)
        assert stages[4].conversion_from_previous == pytest.approx(0.80)

    def test_conversion_from_start_is_a_different_question(self) -> None:
        """§54. A healthy last step is not a healthy funnel, and a
        dashboard conflating them would read one as the other."""
        stages = build_funnel(ACTIVATION_STAGES, self.COUNTS)
        assert stages[4].conversion_from_previous == pytest.approx(0.80)
        assert stages[4].conversion_from_start == pytest.approx(0.40)

    def test_drop_off(self) -> None:
        stages = build_funnel(ACTIVATION_STAGES, self.COUNTS)
        assert [stage.drop_off for stage in stages] == [0, 20, 20, 10, 10]
        assert stages[1].drop_off_rate == pytest.approx(0.20)

    def test_overall_conversion_is_the_last_over_the_first(self) -> None:
        result = FunnelResult(stages=build_funnel(ACTIVATION_STAGES, self.COUNTS), meta=_meta())
        assert result.overall_conversion == pytest.approx(0.40)

    def test_an_empty_funnel_reports_no_conversion_rather_than_zero(self) -> None:
        empty = dict.fromkeys(ACTIVATION_STAGES, 0)
        result = FunnelResult(stages=build_funnel(ACTIVATION_STAGES, empty), meta=_meta())

        assert result.overall_conversion is None
        assert all(stage.conversion_from_previous is None for stage in result.stages[1:])

    def test_drop_off_is_never_negative_for_a_nested_funnel(self) -> None:
        """The SQL nests each stage inside the one before it, so this holds
        by construction. Asserted directly rather than clamped, because a
        clamp would hide the query defect that produced it."""
        stages = build_funnel(ACTIVATION_STAGES, self.COUNTS)
        assert all(stage.drop_off >= 0 for stage in stages)


class TestDurations:
    def test_an_empty_sample_has_no_median(self) -> None:
        """Zero would claim an instant conversion nobody made."""
        summary = DurationSummary(sample=0, median_seconds=None, p95_seconds=None)
        assert summary.median_seconds is None

    def test_the_sample_size_is_reported(self) -> None:
        """A p95 over three people is a number to disbelieve, and a reader
        cannot tell without the count."""
        summary = DurationSummary(sample=3, median_seconds=10.0, p95_seconds=90.0)
        assert summary.sample == 3


class TestDataQuality:
    def test_clean_is_both_counts_at_zero(self) -> None:
        assert DataQuality(out_of_order_subjects=0, completions_without_start=0).is_clean

    def test_either_count_makes_it_unclean(self) -> None:
        assert not DataQuality(out_of_order_subjects=1, completions_without_start=0).is_clean
        assert not DataQuality(out_of_order_subjects=0, completions_without_start=1).is_clean


class TestQualifyingTerminations:
    """§9's matrix, against the real enum.

    Built by exclusion so a member added later qualifies by default and is
    visible in the numbers, rather than silently dropping out of them.
    """

    def test_abort_is_the_only_non_qualifying_reason(self) -> None:
        from app.modules.analytics.domain.properties import TerminationReason
        from app.modules.analytics.infrastructure.repositories.funnel_repository import (
            QUALIFYING_TERMINATIONS,
        )

        assert set(QUALIFYING_TERMINATIONS) == {reason.value for reason in TerminationReason} - {
            "abort"
        }

    def test_a_resignation_qualifies(self) -> None:
        """The one somebody reads as a failure. A64-027.1 §32: a
        resignation is a result, not a game that did not happen."""
        from app.modules.analytics.infrastructure.repositories.funnel_repository import (
            QUALIFYING_TERMINATIONS,
        )

        assert "resignation" in QUALIFYING_TERMINATIONS

    def test_an_abandonment_qualifies_and_is_counted(self) -> None:
        """A result was awarded. It is a **product** failure and not a
        measurement one, and hiding it in a denominator would hide the
        thing worth seeing."""
        from app.modules.analytics.infrastructure.repositories.funnel_repository import (
            QUALIFYING_TERMINATIONS,
        )

        assert "abandonment" in QUALIFYING_TERMINATIONS

    def test_an_abort_does_not(self) -> None:
        from app.modules.analytics.infrastructure.repositories.funnel_repository import (
            QUALIFYING_TERMINATIONS,
        )

        assert "abort" not in QUALIFYING_TERMINATIONS


class TestWindows:
    def test_the_activation_window_fits_inside_retention(self) -> None:
        """§57. A cohort must become mature before its own events are
        pruned, or no cohort is ever both readable and complete."""
        from app.modules.analytics.application.services.retention import RETENTION_DAYS
        from app.modules.analytics.infrastructure.repositories.funnel_repository import (
            ACTIVATION_WINDOW_DAYS,
        )

        assert ACTIVATION_WINDOW_DAYS < RETENTION_DAYS


class TestMaturity:
    """§58, through the service's own clock."""

    def _service(self, oldest: date | None = None):  # type: ignore[no-untyped-def]
        from app.modules.analytics.application.services.funnels import FunnelService

        class _Reader:
            async def acquisition_counts(self, **_: object) -> dict[str, int]:
                return dict.fromkeys(
                    ("landing_viewed", "register_cta_clicked", "user_registered"), 0
                )

            async def activation_counts(self, **_: object) -> dict[str, int]:
                return dict.fromkeys(ACTIVATION_STAGES, 0)

            async def activation_durations(
                self, **_: object
            ) -> dict[str, tuple[int, float | None, float | None]]:
                return {"activation": (0, None, None), "verify": (0, None, None)}

            async def data_quality(self, **_: object) -> DataQuality:
                return DataQuality(out_of_order_subjects=0, completions_without_start=0)

            async def oldest_retained_day(self) -> date | None:
                return oldest

        class _Clock:
            def now(self) -> datetime:
                return NOW

        return FunnelService(reader=_Reader(), clock=_Clock())

    async def test_a_cohort_inside_its_window_is_partial(self) -> None:
        """Its numbers can only rise. Reporting it as mature is how a
        dashboard shows a cliff every day at midnight."""
        today = NOW.date()
        result = await self._service().activation(
            environment="production", since=today, until=today
        )
        assert result.funnel.meta.maturity is Maturity.PARTIAL

    async def test_a_cohort_past_its_window_is_mature(self) -> None:
        old = NOW.date() - timedelta(days=400)
        result = await self._service(oldest=old).activation(
            environment="production", since=old, until=old
        )
        assert result.funnel.meta.maturity is Maturity.MATURE

    async def test_a_range_older_than_retention_is_truncated(self) -> None:
        """§57. A cohort whose beginning was pruned cannot be
        reconstructed, and reporting nought conversions from a deleted
        denominator would be a lie the reader cannot detect."""
        ancient = NOW.date() - timedelta(days=800)
        result = await self._service().activation(
            environment="production", since=ancient, until=NOW.date()
        )

        assert result.funnel.meta.coverage is Coverage.TRUNCATED
        assert result.funnel.meta.requested_from == ancient
        assert result.funnel.meta.cohort_from > ancient

    async def test_a_range_inside_retention_is_complete(self) -> None:
        recent = NOW.date() - timedelta(days=30)
        result = await self._service(oldest=NOW.date() - timedelta(days=100)).activation(
            environment="production", since=recent, until=NOW.date()
        )
        assert result.funnel.meta.coverage is Coverage.COMPLETE

    async def test_a_backwards_range_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ends before"):
            await self._service().activation(
                environment="production", since=date(2026, 2, 1), until=date(2026, 1, 1)
            )
