"""The funnel read port — A64-027.3 §29.

A **data-access** boundary: it returns counts and distributions, and the
service beside it turns them into funnels. The split is where the mistakes
are — a zero denominator, a drop-off against the wrong stage — and those
belong somewhere testable without a database.

There is deliberately no `run_sql(...)`. A general query port would make
every caller a place where the environment filter can be forgotten, and
that is the one filter that must never be: forgetting it puts a laptop's
events in a production number.

Every method takes the environment and the synthetic policy explicitly.
Neither is defaulted here — a default is a thing somebody overrides once
and never checks again.
"""

from datetime import date
from typing import Protocol

from app.modules.analytics.application.read_models.funnels import DataQuality


class FunnelReader(Protocol):
    async def acquisition_counts(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> dict[str, int]:
        """F-A's three stages, keyed by event name.

        Ranged by **event date**, not by cohort: an anonymous visitor has
        no cohort to belong to until they register, which is the whole
        difficulty §14 describes.
        """
        ...

    async def registrations_total(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> int:
        """Every registration in the range, stitched or not.

        Reported beside the acquisition funnel, so its third stage is never
        mistaken for all registrations — the difference between the two is
        the identity stitch's coverage, and §14 requires that gap be
        visible rather than absorbed into a conversion rate.
        """
        ...

    async def activation_counts(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> dict[str, int]:
        """F-B's five stages, keyed by stage name.

        Ranged by **registration cohort** in UTC. Every later stage belongs
        to the registration that began it: an activation in March by
        somebody who registered in January is January's, and a range over
        event dates would credit it to March and leave January looking
        worse than it was.
        """
        ...

    async def activation_durations(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> dict[str, tuple[int, float | None, float | None]]:
        """`(sample, median_seconds, p95_seconds)` for `activation` and
        `verify`, from PostgreSQL's own percentile functions."""
        ...

    async def data_quality(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> DataQuality:
        """What the activation query refused to believe — §40."""
        ...

    async def oldest_retained_day(self) -> date | None:
        """The oldest day raw events still cover.

        Used to mark a result `TRUNCATED` rather than reporting a cohort
        whose beginning was pruned as though it never converted. `None`
        when the store is empty.
        """
        ...
