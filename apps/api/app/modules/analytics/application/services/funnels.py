"""Funnels, from counts to results — A64-027.3.

The SQL counts people per stage; this turns five counts into a funnel and
attaches the provenance that makes the numbers safe to compare. The split
is deliberate: the arithmetic is where the subtle mistakes are — a zero
denominator, a drop-off computed against the wrong stage — and it is
testable without a database.

## Nothing here is stored

An activation rate is a metric, not an event (§44). A funnel result is
computed on request from raw facts, so a corrected projection corrects
every historical number rather than leaving a cached one to disagree with
its own inputs.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from app.core.clock import Clock
from app.modules.analytics.application.ports_read import FunnelReader
from app.modules.analytics.application.read_models.funnels import (
    ActivationSummary,
    Coverage,
    DataQuality,
    DurationSummary,
    FunnelMeta,
    FunnelResult,
    FunnelStage,
    Maturity,
)
from app.modules.analytics.application.services.retention import RETENTION_DAYS
from app.modules.analytics.infrastructure.repositories.funnel_repository import (
    ACQUISITION_WINDOW_DAYS,
    ACTIVATION_WINDOW_DAYS,
)

#: The order the stages are reported in, and the order the SQL enforces.
#: Written here as well as there because a funnel whose stages are returned
#: in dictionary order is a funnel whose conversions are computed against
#: whichever stage happened to come first.
ACQUISITION_STAGES: Final = ("landing_viewed", "register_cta_clicked", "user_registered")
ACTIVATION_STAGES: Final = (
    "user_registered",
    "email_verified",
    "queue_joined",
    "match_started",
    "activated",
)


def rate(part: int, whole: int) -> float | None:
    """A fraction, or `None` when the question has no answer.

    Nought out of nought is **not** zero per cent. A dashboard printing 0%
    there would show a failure that did not happen, which is worse than a
    blank — a blank is read as "no data" and a zero is read as "nobody
    converted".
    """
    if whole <= 0:
        return None
    return part / whole


def build_funnel(stages: tuple[str, ...], counts: dict[str, int]) -> tuple[FunnelStage, ...]:
    """Five counts into a funnel.

    `drop_off` is `previous - current` and cannot be negative, because the
    SQL nests each stage inside the one before it. If it ever were, the
    query is wrong rather than the arithmetic — so this does not clamp it,
    and a test asserts the property directly.
    """
    first = counts[stages[0]]
    built: list[FunnelStage] = []

    for index, stage in enumerate(stages):
        subjects = counts[stage]
        if index == 0:
            built.append(
                FunnelStage(
                    stage=stage,
                    subjects=subjects,
                    conversion_from_previous=None,
                    conversion_from_start=rate(subjects, first),
                    drop_off=0,
                    drop_off_rate=None,
                )
            )
            continue

        previous = counts[stages[index - 1]]
        lost = previous - subjects
        built.append(
            FunnelStage(
                stage=stage,
                subjects=subjects,
                conversion_from_previous=rate(subjects, previous),
                conversion_from_start=rate(subjects, first),
                drop_off=lost,
                drop_off_rate=rate(lost, previous),
            )
        )

    return tuple(built)


@dataclass(frozen=True, slots=True)
class _Range:
    """A requested range, and what survives retention."""

    requested_from: date
    requested_to: date
    effective_from: date
    effective_to: date
    coverage: Coverage


class FunnelService:
    """Acquisition and activation, with their provenance."""

    def __init__(self, *, reader: FunnelReader, clock: Clock) -> None:
        self._reader = reader
        self._clock = clock

    async def acquisition(
        self, *, environment: str, since: date, until: date, include_synthetic: bool = False
    ) -> FunnelResult:
        window = await self._range(since, until, ACQUISITION_WINDOW_DAYS)
        counts = await self._reader.acquisition_counts(
            environment=environment,
            since=window.effective_from,
            until=window.effective_to,
            include_synthetic=include_synthetic,
        )
        total = await self._reader.registrations_total(
            environment=environment,
            since=window.effective_from,
            until=window.effective_to,
            include_synthetic=include_synthetic,
        )
        return FunnelResult(
            stages=build_funnel(ACQUISITION_STAGES, counts),
            meta=self._meta(
                environment,
                include_synthetic,
                window,
                ACQUISITION_WINDOW_DAYS,
                registrations_in_range=total,
            ),
        )

    async def activation(
        self, *, environment: str, since: date, until: date, include_synthetic: bool = False
    ) -> ActivationSummary:
        window = await self._range(since, until, ACTIVATION_WINDOW_DAYS)
        counts = await self._reader.activation_counts(
            environment=environment,
            since=window.effective_from,
            until=window.effective_to,
            include_synthetic=include_synthetic,
        )
        durations = await self._reader.activation_durations(
            environment=environment,
            since=window.effective_from,
            until=window.effective_to,
            include_synthetic=include_synthetic,
        )

        return ActivationSummary(
            funnel=FunnelResult(
                stages=build_funnel(ACTIVATION_STAGES, counts),
                meta=self._meta(environment, include_synthetic, window, ACTIVATION_WINDOW_DAYS),
            ),
            time_to_activation=duration_summary(*durations["activation"]),
            time_to_verify=duration_summary(*durations["verify"]),
        )

    async def data_quality(
        self, *, environment: str, since: date, until: date, include_synthetic: bool = False
    ) -> DataQuality:
        window = await self._range(since, until, ACTIVATION_WINDOW_DAYS)
        return await self._reader.data_quality(
            environment=environment,
            since=window.effective_from,
            until=window.effective_to,
            include_synthetic=include_synthetic,
        )

    # --- provenance -------------------------------------------------------

    async def _range(self, since: date, until: date, window_days: int) -> _Range:
        """Truncates a request to what raw events still cover — §57.

        A cohort older than the retention horizon cannot be reconstructed,
        and a funnel over one would report nought conversions from a
        denominator that was deleted. Reported as `TRUNCATED` with the
        range that survives, rather than silently answered.
        """
        if until < since:
            raise ValueError("the range ends before it begins")

        horizon = self._clock.now().date() - timedelta(days=RETENTION_DAYS)
        oldest = await self._reader.oldest_retained_day()
        floor = max(horizon, oldest) if oldest is not None else horizon

        effective_from = max(since, floor)
        coverage = Coverage.COMPLETE if effective_from <= since else Coverage.TRUNCATED
        return _Range(
            requested_from=since,
            requested_to=until,
            effective_from=effective_from,
            effective_to=until,
            coverage=coverage,
        )

    def _meta(
        self,
        environment: str,
        include_synthetic: bool,
        window: _Range,
        window_days: int,
        *,
        registrations_in_range: int | None = None,
    ) -> FunnelMeta:
        return FunnelMeta(
            environment=environment,
            include_synthetic=include_synthetic,
            cohort_from=window.effective_from,
            cohort_to=window.effective_to,
            requested_from=window.requested_from,
            requested_to=window.requested_to,
            window_days=window_days,
            maturity=self._maturity(window.effective_to, window_days),
            coverage=window.coverage,
            generated_at=self._clock.now(),
            registrations_in_range=registrations_in_range,
        )

    def _maturity(self, cohort_to: date, window_days: int) -> Maturity:
        """Whether the newest cohort in range has had its full window — §58.

        A cohort registered this morning has not, so its activation rate is
        not low: it is unfinished, and the numbers can only rise. Reporting
        the two the same way is how a dashboard shows a cliff every day at
        midnight.
        """
        matured_by = cohort_to + timedelta(days=window_days)
        return Maturity.MATURE if matured_by <= self._clock.now().date() else Maturity.PARTIAL


def duration_summary(sample: int, median: float | None, p95: float | None) -> DurationSummary:
    return DurationSummary(sample=sample, median_seconds=median, p95_seconds=p95)
