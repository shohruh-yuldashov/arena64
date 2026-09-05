"""Engagement and retention arithmetic — A64-027.4.

The SQL counts; this is the half where the subtle mistakes live, and the
subtlest is the difference between "nobody came back" and "we have not
looked yet". Both are the absence of a number, and only one of them is a
zero.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.modules.analytics.application.read_models.engagement import (
    ActivePlayers,
    RetentionRow,
    RetentionTable,
)
from app.modules.analytics.application.read_models.funnels import (
    Coverage,
    FunnelMeta,
    Maturity,
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
        window_days=30,
        maturity=Maturity.MATURE,
        coverage=Coverage.COMPLETE,
        generated_at=NOW,
    )


class TestActivePlayers:
    def test_stickiness_is_daily_over_monthly(self) -> None:
        players = ActivePlayers(as_of=NOW.date(), daily=20, weekly=60, monthly=100)
        assert players.stickiness == pytest.approx(0.20)

    def test_an_empty_month_has_no_stickiness(self) -> None:
        """`0.0` would read as an audience that never returns rather than
        one that does not exist yet."""
        players = ActivePlayers(as_of=NOW.date(), daily=0, weekly=0, monthly=0)
        assert players.stickiness is None


class TestRetentionRows:
    def test_a_rate_is_retained_over_cohort(self) -> None:
        row = RetentionRow(cohort_day=date(2026, 1, 1), cohort=100, d1=40, d7=25, d30=10)
        assert row.rate(1) == pytest.approx(0.40)
        assert row.rate(7) == pytest.approx(0.25)
        assert row.rate(30) == pytest.approx(0.10)

    def test_an_unelapsed_window_is_none_not_zero(self) -> None:
        """A64-027.1 §33: "a partial D7 is always wrong and always looks
        like a decline". A cohort from yesterday has not failed its D30 —
        it has not had one."""
        row = RetentionRow(cohort_day=NOW.date(), cohort=50, d1=None, d7=None, d30=None)
        assert row.rate(1) is None
        assert row.rate(30) is None

    def test_an_empty_cohort_has_no_rate(self) -> None:
        row = RetentionRow(cohort_day=date(2026, 1, 1), cohort=0, d1=0, d7=0, d30=0)
        assert row.rate(1) is None

    def test_nobody_returning_is_zero_and_not_none(self) -> None:
        """The distinction this whole type exists for: a measured nought is
        a number, and an unmeasured one is not."""
        row = RetentionRow(cohort_day=date(2026, 1, 1), cohort=10, d1=0, d7=0, d30=0)
        assert row.rate(1) == 0.0


class TestMatureRows:
    def test_it_returns_only_the_cohorts_that_finished(self) -> None:
        table = RetentionTable(
            rows=(
                RetentionRow(cohort_day=date(2026, 1, 1), cohort=10, d1=5, d7=3, d30=1),
                RetentionRow(cohort_day=date(2026, 9, 4), cohort=10, d1=None, d7=None, d30=None),
            ),
            meta=_meta(),
        )

        assert len(table.mature_rows(1)) == 1
        assert table.mature_rows(1)[0].cohort_day == date(2026, 1, 1)

    def test_a_cohort_can_be_mature_for_d1_and_not_for_d30(self) -> None:
        """Every row matures column by column, which is why maturity is per
        row rather than per table."""
        table = RetentionTable(
            rows=(RetentionRow(cohort_day=date(2026, 9, 1), cohort=10, d1=4, d7=2, d30=None),),
            meta=_meta(),
        )

        assert len(table.mature_rows(1)) == 1
        assert len(table.mature_rows(7)) == 1
        assert len(table.mature_rows(30)) == 0


class TestTheActivityDefinition:
    def test_it_is_exactly_the_three_events_frozen_in_the_document(self) -> None:
        """A64-027.1 §30. Written out here rather than imported into the
        assertion, so this test disagrees with the code when the code
        changes — which is the only way it can catch a fourth signal being
        added without a decision."""
        from app.modules.analytics.infrastructure.repositories.engagement_repository import (
            ACTIVITY_EVENTS,
        )

        assert set(ACTIVITY_EVENTS) == {
            "match_started",
            "tournament_entered",
            "challenge_sent",
        }

    def test_opening_a_page_is_not_activity(self) -> None:
        """If it were, DAU would measure the landing page and a marketing
        campaign would look like engagement."""
        from app.modules.analytics.infrastructure.repositories.engagement_repository import (
            ACTIVITY_EVENTS,
        )

        assert "landing_viewed" not in ACTIVITY_EVENTS

    def test_completing_a_match_is_not_the_signal_starting_one_is(self) -> None:
        """§30: somebody who started a game and lost connection used the
        product."""
        from app.modules.analytics.infrastructure.repositories.engagement_repository import (
            ACTIVITY_EVENTS,
        )

        assert "match_started" in ACTIVITY_EVENTS
        assert "match_completed" not in ACTIVITY_EVENTS


class TestRetentionOffsets:
    def test_they_are_the_three_the_document_names(self) -> None:
        from app.modules.analytics.infrastructure.repositories.engagement_repository import (
            RETENTION_DAYS_OFFSETS,
        )

        assert RETENTION_DAYS_OFFSETS == (1, 7, 30)

    def test_the_longest_fits_inside_raw_retention(self) -> None:
        """A cohort must reach its D30 before its own events are pruned."""
        from app.modules.analytics.application.services.retention import RETENTION_DAYS
        from app.modules.analytics.infrastructure.repositories.engagement_repository import (
            RETENTION_DAYS_OFFSETS,
        )

        assert max(RETENTION_DAYS_OFFSETS) < RETENTION_DAYS


class TestEngagementSummaryRates:
    def _summary(self, **overrides: object):  # type: ignore[no-untyped-def]
        from app.modules.analytics.application.read_models.engagement import EngagementSummary

        defaults: dict[str, object] = {
            "week_start": date(2026, 1, 1),
            "week_end": date(2026, 1, 7),
            "meta": _meta(),
            "active_players": 100,
            "match_starts": 250,
            "matches_per_active_player": 2.5,
            "median_matches_per_active_player": 2.0,
            "tournament_entrants": 20,
            "tournament_participation": 0.2,
            "friendships_created": 15,
            "challenges_sent": 40,
            "challenges_accepted": 30,
            "challenges_declined": 5,
            "challenges_expired": 4,
            "challenges_cancelled": 1,
        }
        defaults.update(overrides)
        return EngagementSummary(**defaults)  # type: ignore[arg-type]

    def test_challenge_acceptance(self) -> None:
        assert self._summary().challenge_acceptance == pytest.approx(0.75)

    def test_no_challenges_means_no_rate(self) -> None:
        assert self._summary(challenges_sent=0).challenge_acceptance is None

    def test_the_three_refusals_stay_separate(self) -> None:
        """A64-027.1 §29: expiry and decline are different product
        problems, and merging them into "not accepted" loses which one."""
        summary = self._summary()
        assert (summary.challenges_declined, summary.challenges_expired) == (5, 4)
        assert summary.challenges_cancelled == 1

    def test_the_mean_and_the_median_are_both_reported(self) -> None:
        """§29's limitation on M22: a handful of people play a great deal,
        and a mean over that describes nobody."""
        summary = self._summary()
        assert summary.matches_per_active_player == pytest.approx(2.5)
        assert summary.median_matches_per_active_player == pytest.approx(2.0)


class TestServiceDefaults:
    def _service(self, rows: list[dict[str, object]] | None = None):  # type: ignore[no-untyped-def]
        from app.modules.analytics.application.services.engagement import EngagementService

        class _Reader:
            def __init__(self) -> None:
                self.as_of: date | None = None

            async def active_players(self, *, as_of: date, **_: object) -> dict[str, int]:
                self.as_of = as_of
                return {"daily": 1, "weekly": 2, "monthly": 3}

            async def retention(self, **_: object) -> list[dict[str, object]]:
                return rows or []

            async def engagement(self, **_: object) -> dict[str, object]:
                return {
                    "active_players": 0,
                    "match_starts": 0,
                    "median_matches": None,
                    "tournament_entrants": 0,
                    "friendships": 0,
                    "challenges_sent": 0,
                    "accepted": 0,
                    "declined": 0,
                    "expired": 0,
                    "cancelled": 0,
                }

        class _Clock:
            def now(self) -> datetime:
                return NOW

        reader = _Reader()
        return EngagementService(reader=reader, clock=_Clock()), reader

    async def test_active_players_defaults_to_yesterday(self) -> None:
        """A partial day rises all day and looks like a collapse every
        morning — the same mistake as a partial cohort."""
        service, reader = self._service()
        result = await service.active_players(environment="production")

        assert reader.as_of == NOW.date() - timedelta(days=1)
        assert result.as_of == NOW.date() - timedelta(days=1)

    async def test_a_backwards_retention_range_is_refused(self) -> None:
        service, _ = self._service()
        with pytest.raises(ValueError, match="ends before"):
            await service.retention(
                environment="production", since=date(2026, 2, 1), until=date(2026, 1, 1)
            )

    async def test_a_range_older_than_raw_retention_is_truncated(self) -> None:
        service, _ = self._service()
        ancient = NOW.date() - timedelta(days=800)

        table = await service.retention(environment="production", since=ancient, until=NOW.date())

        assert table.meta.coverage is Coverage.TRUNCATED
        assert table.meta.requested_from == ancient
        assert table.meta.cohort_from > ancient

    async def test_a_week_that_has_not_ended_is_partial(self) -> None:
        service, _ = self._service()
        summary = await service.engagement(
            environment="production", week_start=NOW.date() - timedelta(days=2)
        )
        assert summary.meta.maturity is Maturity.PARTIAL

    async def test_a_finished_week_is_mature(self) -> None:
        service, _ = self._service()
        summary = await service.engagement(
            environment="production", week_start=NOW.date() - timedelta(days=30)
        )
        assert summary.meta.maturity is Maturity.MATURE
