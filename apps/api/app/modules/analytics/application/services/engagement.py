"""Engagement and retention, from counts to results — A64-027.4.

The same split Part III established: the SQL counts, this does the
arithmetic and attaches the provenance. Nothing is stored — a retention rate
is a metric, so a corrected projection corrects every historical number.
"""

from datetime import date, timedelta

from app.core.clock import Clock
from app.modules.analytics.application.ports_read import EngagementReader
from app.modules.analytics.application.read_models.engagement import (
    ActivePlayers,
    EngagementSummary,
    RetentionRow,
    RetentionTable,
)
from app.modules.analytics.application.read_models.funnels import (
    Coverage,
    FunnelMeta,
    Maturity,
)
from app.modules.analytics.application.services.funnels import rate
from app.modules.analytics.application.services.retention import RETENTION_DAYS
from app.modules.analytics.infrastructure.repositories.engagement_repository import (
    RETENTION_DAYS_OFFSETS,
)


class EngagementService:
    """DAU/WAU/MAU, retention cohorts, and the weekly engagement metrics."""

    def __init__(self, *, reader: EngagementReader, clock: Clock) -> None:
        self._reader = reader
        self._clock = clock

    async def active_players(
        self, *, environment: str, as_of: date | None = None, include_synthetic: bool = False
    ) -> ActivePlayers:
        """A64-027.1 §30's definition over 1, 7 and 30 days.

        `as_of` defaults to **yesterday**, not today. A partial day is the
        same mistake as a partial cohort: today's DAU rises all day and
        looks like a collapse every morning.
        """
        day = as_of if as_of is not None else self._clock.now().date() - timedelta(days=1)
        counts = await self._reader.active_players(
            environment=environment, as_of=day, include_synthetic=include_synthetic
        )
        return ActivePlayers(
            as_of=day,
            daily=counts["daily"],
            weekly=counts["weekly"],
            monthly=counts["monthly"],
        )

    async def retention(
        self, *, environment: str, since: date, until: date, include_synthetic: bool = False
    ) -> RetentionTable:
        """One row per registration cohort day.

        The range is truncated to what retention still covers, for the
        reason Part III gives: a cohort whose beginning was pruned would
        report nought returners from a denominator that was deleted.
        """
        if until < since:
            raise ValueError("the range ends before it begins")

        today = self._clock.now().date()
        floor = today - timedelta(days=RETENTION_DAYS)
        effective_from = max(since, floor)
        coverage = Coverage.COMPLETE if effective_from <= since else Coverage.TRUNCATED

        rows = await self._reader.retention(
            environment=environment,
            since=effective_from,
            until=until,
            include_synthetic=include_synthetic,
            today=today,
        )

        built = tuple(
            RetentionRow(
                cohort_day=row["cohort_day"],
                cohort=row["cohort"],
                d1=row["d1"],
                d7=row["d7"],
                d30=row["d30"],
            )
            for row in rows
        )

        return RetentionTable(
            rows=built,
            meta=FunnelMeta(
                environment=environment,
                include_synthetic=include_synthetic,
                cohort_from=effective_from,
                cohort_to=until,
                requested_from=since,
                requested_to=until,
                # The longest window a row in this table reports on.
                window_days=max(RETENTION_DAYS_OFFSETS),
                maturity=self._maturity(until, max(RETENTION_DAYS_OFFSETS), today),
                coverage=coverage,
                generated_at=self._clock.now(),
            ),
        )

    async def engagement(
        self,
        *,
        environment: str,
        week_start: date,
        include_synthetic: bool = False,
    ) -> EngagementSummary:
        """One calendar week, starting on `week_start` (inclusive).

        A week rather than a rolling seven days, because A64-027.1 §29
        specifies a weekly window for all four of these and a rolling one
        would make two adjacent readings share six days of data.
        """
        week_end = week_start + timedelta(days=6)
        today = self._clock.now().date()
        counts = await self._reader.engagement(
            environment=environment,
            week_start=week_start,
            week_end=week_end,
            include_synthetic=include_synthetic,
        )

        active = counts["active_players"]
        return EngagementSummary(
            week_start=week_start,
            week_end=week_end,
            meta=FunnelMeta(
                environment=environment,
                include_synthetic=include_synthetic,
                cohort_from=week_start,
                cohort_to=week_end,
                requested_from=week_start,
                requested_to=week_end,
                window_days=7,
                maturity=self._maturity(week_end, 0, today),
                coverage=Coverage.COMPLETE
                if week_start >= today - timedelta(days=RETENTION_DAYS)
                else Coverage.TRUNCATED,
                generated_at=self._clock.now(),
            ),
            active_players=active,
            match_starts=counts["match_starts"],
            matches_per_active_player=rate(counts["match_starts"], active),
            median_matches_per_active_player=counts["median_matches"],
            tournament_entrants=counts["tournament_entrants"],
            tournament_participation=rate(counts["tournament_entrants"], active),
            friendships_created=counts["friendships"],
            challenges_sent=counts["challenges_sent"],
            challenges_accepted=counts["accepted"],
            challenges_declined=counts["declined"],
            challenges_expired=counts["expired"],
            challenges_cancelled=counts["cancelled"],
        )

    def _maturity(self, window_end: date, window_days: int, today: date) -> Maturity:
        """Whether the last day the result covers is finished.

        `window_days` is nought for a week: the week is mature the day after
        it ends. For a retention table it is thirty, because the newest
        cohort needs its D30 before the table is finished.
        """
        return (
            Maturity.MATURE
            if window_end + timedelta(days=window_days) < today
            else Maturity.PARTIAL
        )
