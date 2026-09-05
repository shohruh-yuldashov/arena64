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
from typing import Any, Protocol

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


class EngagementReader(Protocol):
    """A64-027.4's data-access boundary. Counts in, arithmetic elsewhere."""

    async def active_players(
        self, *, environment: str, as_of: date, include_synthetic: bool
    ) -> dict[str, int]:
        """`daily`, `weekly` and `monthly` over A64-027.1 §30's activity
        definition, all three ending on `as_of` so they are comparable."""
        ...

    async def retention(
        self, *, environment: str, since: date, until: date, include_synthetic: bool, today: date
    ) -> list[dict[str, Any]]:
        """One mapping per registration cohort day: `cohort_day`, `cohort`,
        `d1`, `d7`, `d30`.

        `today` is passed in rather than read from the database clock, so
        which cohorts count as mature is decided by the application's
        injected clock and a test can freeze it.
        """
        ...

    async def engagement(
        self, *, environment: str, week_start: date, week_end: date, include_synthetic: bool
    ) -> dict[str, Any]:
        """M15, M16, M17 and M22's raw counts over one week."""
        ...


class MatchmakingReader(Protocol):
    """A64-027.5's data-access boundary — queue, offer and game health.

    A reader of its own rather than more methods on `FunnelReader`: these
    answer a different question at a different grain, and one port holding
    every analytics query would be the god object §42 warns about.
    """

    async def queue_health(
        self,
        *,
        environment: str,
        since: date,
        until: date,
        include_synthetic: bool,
        rated: bool | None = None,
    ) -> dict[str, Any]:
        """M6, M7, M7b and M8's raw counts, at queue-attempt grain."""
        ...

    async def offer_health(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> dict[str, int]:
        """M9's three outcomes, at offer grain."""
        ...

    async def game_health(
        self,
        *,
        environment: str,
        since: date,
        until: date,
        include_synthetic: bool,
        rated: bool | None = None,
        speed_class: str | None = None,
    ) -> dict[str, Any]:
        """M10 – M14's raw counts, at **match** grain."""
        ...

    async def data_quality(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> dict[str, int]:
        """Lifecycle anomalies, counted and never repaired."""
        ...
