"""Matchmaking and game health — A64-027.5.

The repository counts; this attaches the grain and the provenance. Nothing
is stored: every rate is a metric, so a corrected projection corrects every
historical number.

## Segmentation is validated, not interpolated

`speed_class` reaches SQL as a string. It is checked against the analytics
vocabulary here first, so a caller cannot put arbitrary text into a query —
the value is a member of a closed enum by the time the repository sees it.
"""

from datetime import date, timedelta

from app.core.clock import Clock
from app.modules.analytics.application.ports_read import MatchmakingReader
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
from app.modules.analytics.application.services.retention import RETENTION_DAYS
from app.modules.analytics.domain.properties import SpeedClass


class MatchmakingService:
    """Queue, offer and game health over a bounded window."""

    def __init__(self, *, reader: MatchmakingReader, clock: Clock) -> None:
        self._reader = reader
        self._clock = clock

    async def queue_health(
        self,
        *,
        environment: str,
        since: date,
        until: date,
        include_synthetic: bool = False,
        rated: bool | None = None,
    ) -> QueueHealth:
        self._check_range(since, until)
        counts = await self._reader.queue_health(
            environment=environment,
            since=since,
            until=until,
            include_synthetic=include_synthetic,
            rated=rated,
        )
        return QueueHealth(
            grain=Grain.QUEUE_ATTEMPT,
            period_start=since,
            period_end=until,
            meta=self._meta(environment, include_synthetic, since, until),
            queue_joins=counts["joins"],
            paired_attempts=counts["paired"],
            abandoned_attempts=counts["abandoned"],
            cancelled_attempts=counts["cancelled"],
            expired_attempts=counts["expired"],
            wait=WaitDistribution(
                sample=counts["wait_sample"],
                p50_seconds=counts["wait_p50"],
                p95_seconds=counts["wait_p95"],
            ),
        )

    async def offer_health(
        self, *, environment: str, since: date, until: date, include_synthetic: bool = False
    ) -> OfferHealth:
        self._check_range(since, until)
        counts = await self._reader.offer_health(
            environment=environment,
            since=since,
            until=until,
            include_synthetic=include_synthetic,
        )
        return OfferHealth(
            grain=Grain.OFFER,
            period_start=since,
            period_end=until,
            meta=self._meta(environment, include_synthetic, since, until),
            accepted=counts["accepted"],
            declined=counts["declined"],
            expired=counts["expired"],
        )

    async def game_health(
        self,
        *,
        environment: str,
        since: date,
        until: date,
        include_synthetic: bool = False,
        rated: bool | None = None,
        speed_class: SpeedClass | None = None,
    ) -> GameHealth:
        """M10 – M14, optionally segmented.

        `speed_class` is the **enum**, not a string: the type is the
        validation, so nothing a caller types reaches the query.
        """
        self._check_range(since, until)
        counts = await self._reader.game_health(
            environment=environment,
            since=since,
            until=until,
            include_synthetic=include_synthetic,
            rated=rated,
            speed_class=speed_class.value if speed_class is not None else None,
        )
        return GameHealth(
            grain=Grain.MATCH,
            period_start=since,
            period_end=until,
            meta=self._meta(environment, include_synthetic, since, until),
            started=counts["started"],
            completed=counts["completed"],
            aborted=counts["aborted"],
            resignations=counts["resignations"],
            draws=counts["draws"],
            abandonments=counts["abandonments"],
            flags=counts["flags"],
            rated_completions=counts["rated_completions"],
            termination_breakdown=counts["breakdown"],
        )

    async def data_quality(
        self, *, environment: str, since: date, until: date, include_synthetic: bool = False
    ) -> dict[str, int]:
        self._check_range(since, until)
        return await self._reader.data_quality(
            environment=environment,
            since=since,
            until=until,
            include_synthetic=include_synthetic,
        )

    # --- provenance -------------------------------------------------------

    def _check_range(self, since: date, until: date) -> None:
        if until < since:
            raise ValueError("the range ends before it begins")

    def _meta(
        self, environment: str, include_synthetic: bool, since: date, until: date
    ) -> FunnelMeta:
        """Provenance, with **no maturity window**.

        These are event-dated counts rather than cohorts: a day's queue
        health is finished when the day is. So `PARTIAL` means only that
        the range reaches today, and `window_days` is nought because there
        is no conversion window to wait for — §63's warning against
        inventing retention-style maturity where it does not apply.
        """
        today = self._clock.now().date()
        return FunnelMeta(
            environment=environment,
            include_synthetic=include_synthetic,
            cohort_from=max(since, today - timedelta(days=RETENTION_DAYS)),
            cohort_to=until,
            requested_from=since,
            requested_to=until,
            window_days=0,
            maturity=Maturity.MATURE if until < today else Maturity.PARTIAL,
            coverage=Coverage.COMPLETE
            if since >= today - timedelta(days=RETENTION_DAYS)
            else Coverage.TRUNCATED,
            generated_at=self._clock.now(),
        )
