"""The funnel queries — A64-027.3 §41.

**Set-based, in PostgreSQL.** Nothing here loads events into Python and
counts them: a funnel over a year of a busy platform is millions of rows,
and the database is the thing that can answer "how many distinct people
reached this stage" without materialising them.

## The one idea the activation query is built on

A funnel is not five independent counts. Counting `email_verified` rows and
calling it the second stage would include somebody whose registration is
outside the range, or absent, or later than their verification — and the
number would look entirely reasonable.

So the query starts from **one row per registered subject** and left-joins
each later stage onto it, keeping only the events that belong to *that*
registration: same subject, at or after the registration instant, inside the
conversion window. Every stage is then a subset of the one before it by
construction, which is what makes `drop_off` non-negative without a
`GREATEST(0, …)` hiding a bug.

## Activation needs a join, because a completion has no player

`match_completed` is entity-level — A64-027.1 §18: one game has two
perspectives, and attributing it to one seat would count the game for one
player and lose it for the other. So it knows what happened and not to whom.

`match_started` supplies the other half, one row per seat. Activation is the
earliest completion whose match this subject started, with an abort
excluded. Derived at query time and never stored, per §44.
"""

from datetime import date, datetime, time, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.application.read_models.funnels import DataQuality
from app.modules.analytics.domain.properties import TerminationReason

#: A64-027.1 §32. Every termination but `abort` is a game played to a
#: result — a resignation is a result, an abandonment is a result somebody
#: was awarded, and a flag is a legitimate loss on time. `abort` is
#: `MatchOutcome.NONE`: no result, no rating change, a match that did not
#: happen.
#:
#: Built by **exclusion** rather than by listing the ten, so a termination
#: reason added later qualifies by default and shows up in the numbers
#: where somebody can see it. The mapping test asserts the enum is total,
#: so a new member cannot arrive unnoticed.
NON_QUALIFYING: Final = frozenset({TerminationReason.ABORT.value})

QUALIFYING_TERMINATIONS: Final = tuple(
    sorted(reason.value for reason in TerminationReason if reason.value not in NON_QUALIFYING)
)

#: **7 days**, frozen by A64-027.1 §34's F-B and by M19's formula
#: ("activated within 7 days / registered"). Not a number this task chose:
#: A64-027.3 first implemented 365 and the drift was caught by re-reading
#: the metric table, which is what §63 asks for.
#:
#: Seven is also the window that makes a cohort readable quickly — it
#: matures a week after registration rather than a year — and it sits far
#: inside the 400-day retention horizon, so a cohort is always mature
#: before its own events are pruned.
ACTIVATION_WINDOW_DAYS: Final = 7

#: The acquisition funnel's window. A landing view and the registration it
#: leads to happen in one sitting or not at all; a day is generous.
ACQUISITION_WINDOW_DAYS: Final = 1


def day_start(day: date) -> datetime:
    """Midnight UTC. §56 — a cohort boundary is never a browser's idea of a
    day, and `date` alone would compare against a naive instant."""
    from datetime import UTC

    return datetime.combine(day, time.min, tzinfo=UTC)


def day_end(day: date) -> datetime:
    """The first instant of the following day, so a range is half-open and
    23:59:59.999 belongs to the day it looks like."""
    return day_start(day) + timedelta(days=1)


class SqlAlchemyFunnelReader:
    """The four queries, over one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- acquisition ------------------------------------------------------

    async def acquisition_counts(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> dict[str, int]:
        """Three stages, counted over **anonymous ids**.

        The subject of an acquisition funnel is a browser: nobody has an
        account yet at the first two stages, and the third is where they
        get one. So the distinct key is `anonymous_id`, and the third stage
        is the registrations that a *known* browser produced — see §14 on
        why that number is smaller than total registrations and why both
        are reported.

        Strictly nested, like the activation query: an intent counts only
        for a browser that was seen landing, and a registration only for a
        browser that showed intent.
        """
        statement = text(f"""
            WITH landed AS (
                SELECT DISTINCT anonymous_id, MIN(occurred_at) AS at
                FROM analytics.event
                WHERE event_name = 'landing_viewed'
                  AND environment = :environment
                  {self._synthetic_clause(include_synthetic)}
                  AND occurred_at >= :since AND occurred_at < :until
                  AND anonymous_id IS NOT NULL
                GROUP BY anonymous_id
            ),
            intent AS (
                SELECT l.anonymous_id, MIN(e.occurred_at) AS at
                FROM landed l
                JOIN analytics.event e
                  ON e.anonymous_id = l.anonymous_id
                 AND e.event_name = 'register_cta_clicked'
                 AND e.environment = :environment
                 {self._synthetic_clause(include_synthetic, alias="e")}
                 AND e.occurred_at >= l.at
                 AND e.occurred_at < l.at + make_interval(days => :window)
                GROUP BY l.anonymous_id
            ),
            -- The identity stitch, **at query time** — A64-027.1 §9.
            --
            -- `user_registered` is a backend projection: it carries a
            -- subject and no `anonymous_id`, because the server never saw
            -- the browser. The only rows holding both are client events
            -- fired by a signed-in player, which the collector stamps with
            -- the session's subject and the body's browser id.
            --
            -- So the link is *derived* from those rows rather than stored,
            -- and raw history is never rewritten — the decision A64-027.1
            -- froze and §14 of this task repeats. Its coverage is the
            -- honest problem: see `acquisition_coverage`.
            link AS (
                SELECT DISTINCT anonymous_id, subject_key
                FROM analytics.event
                WHERE anonymous_id IS NOT NULL
                  AND subject_key IS NOT NULL
                  AND environment = :environment
                  {self._synthetic_clause(include_synthetic)}
            ),
            registered AS (
                SELECT i.anonymous_id
                FROM intent i
                JOIN link l ON l.anonymous_id = i.anonymous_id
                JOIN analytics.event e
                  ON e.subject_key = l.subject_key
                 AND e.event_name = 'user_registered'
                 AND e.environment = :environment
                 {self._synthetic_clause(include_synthetic, alias="e")}
                 AND e.occurred_at >= i.at
                 AND e.occurred_at < i.at + make_interval(days => :window)
                GROUP BY i.anonymous_id
            )
            SELECT
                (SELECT count(*) FROM landed)     AS landed,
                (SELECT count(*) FROM intent)     AS intent,
                (SELECT count(*) FROM registered) AS registered
        """)
        row = (
            await self._session.execute(
                statement,
                {
                    "environment": environment,
                    "since": day_start(since),
                    "until": day_end(until),
                    "window": ACQUISITION_WINDOW_DAYS,
                },
            )
        ).one()
        return {
            "landing_viewed": row.landed,
            "register_cta_clicked": row.intent,
            "user_registered": row.registered,
        }

    async def registrations_total(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> int:
        """Every registration in the range, stitched or not.

        Reported beside the acquisition funnel so the third stage's number
        is never mistaken for all of them. The difference between the two
        **is** the stitch's coverage, and §14 requires that gap be visible
        rather than absorbed into a conversion rate.
        """
        statement = text(f"""
            SELECT count(DISTINCT subject_key) AS total
            FROM analytics.event
            WHERE event_name = 'user_registered'
              AND environment = :environment
              {self._synthetic_clause(include_synthetic)}
              AND occurred_at >= :since AND occurred_at < :until
              AND subject_key IS NOT NULL
        """)
        row = (
            await self._session.execute(
                statement,
                {
                    "environment": environment,
                    "since": day_start(since),
                    "until": day_end(until),
                },
            )
        ).one()
        total: int = row.total
        return total

    # --- activation -------------------------------------------------------

    async def activation_counts(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> dict[str, int]:
        """Five stages over a registration cohort, strictly nested.

        `cohort` is one row per registered subject with their registration
        instant. Every later stage is a `LEFT JOIN LATERAL` that looks only
        at that subject's events, at or after that instant, inside the
        window — so a stage cannot be reached by somebody who did not reach
        the one before it, and `COUNT(...)` over the joined column is the
        stage's size.
        """
        row = (
            await self._session.execute(
                text(self._activation_sql(include_synthetic)),
                self._activation_params(environment, since, until),
            )
        ).one()
        return {
            "user_registered": row.registered,
            "email_verified": row.verified,
            "queue_joined": row.queued,
            "match_started": row.started,
            "activated": row.activated,
        }

    async def activation_durations(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> dict[str, tuple[int, float | None, float | None]]:
        """Median and p95, in seconds, from PostgreSQL's own percentiles.

        Never an average of anything (§55). Both durations are between two
        **server** instants — a registration and a completion — so no client
        clock is involved and a negative value is impossible by the query's
        own ordering constraint rather than by filtering afterwards.
        """
        statement = text(f"""
            {self._cohort_cte(include_synthetic)},
            {self._verified_cte(include_synthetic)},
            {self._started_cte(include_synthetic)},
            {self._activated_cte(include_synthetic)}
            SELECT
                count(a.at) AS activated_sample,
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (a.at - c.registered_at))
                ) FILTER (WHERE a.at IS NOT NULL) AS activation_median,
                percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (a.at - c.registered_at))
                ) FILTER (WHERE a.at IS NOT NULL) AS activation_p95,
                count(v.at) AS verified_sample,
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (v.at - c.registered_at))
                ) FILTER (WHERE v.at IS NOT NULL) AS verify_median,
                percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (v.at - c.registered_at))
                ) FILTER (WHERE v.at IS NOT NULL) AS verify_p95
            FROM cohort c
            LEFT JOIN verified v ON v.subject_key = c.subject_key
            LEFT JOIN started s ON s.subject_key = c.subject_key
            LEFT JOIN activated a ON a.subject_key = c.subject_key
        """)
        row = (
            await self._session.execute(
                statement, self._activation_params(environment, since, until)
            )
        ).one()
        return {
            "activation": (
                row.activated_sample,
                _seconds(row.activation_median),
                _seconds(row.activation_p95),
            ),
            "verify": (row.verified_sample, _seconds(row.verify_median), _seconds(row.verify_p95)),
        }

    # --- data quality -----------------------------------------------------

    async def data_quality(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> DataQuality:
        """Two impossibilities, counted rather than repaired — §39.

        Raw events are never edited. A journey the query cannot believe is
        excluded from the stages by the ordering constraint that already
        exists, and counted here so the exclusion is visible instead of
        looking like a conversion nobody made.
        """
        statement = text(f"""
            {self._cohort_cte(include_synthetic)},
            out_of_order AS (
                SELECT DISTINCT c.subject_key
                FROM cohort c
                JOIN analytics.event e
                  ON e.subject_key = c.subject_key
                 AND e.environment = :environment
                 AND e.event_name IN ('email_verified', 'queue_joined', 'match_started')
                 AND e.occurred_at < c.registered_at
            ),
            orphan_completions AS (
                SELECT DISTINCT done.id
                FROM analytics.event done
                WHERE done.event_name = 'match_completed'
                  AND done.environment = :environment
                  AND done.occurred_at >= :since AND done.occurred_at < :until
                  AND NOT EXISTS (
                      SELECT 1 FROM analytics.event s
                      WHERE s.event_name = 'match_started'
                        AND s.environment = :environment
                        AND s.properties->>'match_id' = done.properties->>'match_id'
                  )
            )
            SELECT
                (SELECT count(*) FROM out_of_order)       AS out_of_order,
                (SELECT count(*) FROM orphan_completions) AS orphans
        """)
        row = (
            await self._session.execute(
                statement, self._activation_params(environment, since, until)
            )
        ).one()
        return DataQuality(
            out_of_order_subjects=row.out_of_order,
            completions_without_start=row.orphans,
        )

    async def oldest_retained_day(self) -> date | None:
        row = (
            await self._session.execute(
                text("SELECT MIN(occurred_at)::date AS oldest FROM analytics.event")
            )
        ).one()
        oldest: date | None = row.oldest
        return oldest

    # --- the shared CTEs --------------------------------------------------

    def _synthetic_clause(self, include_synthetic: bool, *, alias: str = "") -> str:
        """§26. Excluded by default, and the filter is **in every CTE**.

        A single filter on the cohort would let a synthetic player's later
        events attach to a real registration — which cannot happen today,
        but would the moment somebody wrote a query that started elsewhere.
        """
        if include_synthetic:
            return ""
        prefix = f"{alias}." if alias else ""
        return f"AND {prefix}is_synthetic = false"

    def _cohort_cte(self, include_synthetic: bool) -> str:
        """One row per registered subject, with their registration instant.

        `MIN`, because a subject has exactly one registration and a `MIN`
        over one row costs nothing while making a second one — which would
        be a defect — harmless rather than a duplicated cohort member.
        """
        return f"""
            WITH cohort AS (
                SELECT subject_key, MIN(occurred_at) AS registered_at
                FROM analytics.event
                WHERE event_name = 'user_registered'
                  AND environment = :environment
                  {self._synthetic_clause(include_synthetic)}
                  AND occurred_at >= :since AND occurred_at < :until
                  AND subject_key IS NOT NULL
                GROUP BY subject_key
            )"""

    def _stage_cte(self, name: str, event_name: str, include_synthetic: bool) -> str:
        """A stage that is a subset of the cohort, by construction.

        The three constraints are the funnel: same subject, at or after the
        registration, and inside the window. Drop any one and the stage
        starts counting people it should not.
        """
        return f"""
            {name} AS (
                SELECT c.subject_key, MIN(e.occurred_at) AS at
                FROM cohort c
                JOIN analytics.event e
                  ON e.subject_key = c.subject_key
                 AND e.event_name = '{event_name}'
                 AND e.environment = :environment
                 {self._synthetic_clause(include_synthetic, alias="e")}
                 AND e.occurred_at >= c.registered_at
                 AND e.occurred_at < c.registered_at + make_interval(days => :window)
                GROUP BY c.subject_key
            )"""

    def _verified_cte(self, include_synthetic: bool) -> str:
        return self._stage_cte("verified", "email_verified", include_synthetic)

    def _queued_cte(self, include_synthetic: bool) -> str:
        return self._stage_cte("queued", "queue_joined", include_synthetic)

    def _started_cte(self, include_synthetic: bool) -> str:
        return self._stage_cte("started", "match_started", include_synthetic)

    def _activated_cte(self, include_synthetic: bool) -> str:
        """The join a completion needs, because it carries no player.

        `match_started` supplies `(subject, match_id)`; `match_completed`
        supplies `(match_id, termination_reason)`. The earliest completion
        whose match this subject started, excluding an abort, **is** the
        activation — A64-027.1 §31, derived rather than stored.
        """
        qualifying = ", ".join(f"'{reason}'" for reason in QUALIFYING_TERMINATIONS)
        return f"""
            activated AS (
                SELECT s.subject_key, MIN(done.occurred_at) AS at
                FROM cohort c
                JOIN analytics.event s
                  ON s.subject_key = c.subject_key
                 AND s.event_name = 'match_started'
                 AND s.environment = :environment
                 {self._synthetic_clause(include_synthetic, alias="s")}
                 AND s.occurred_at >= c.registered_at
                 AND s.occurred_at < c.registered_at + make_interval(days => :window)
                JOIN analytics.event done
                  ON done.event_name = 'match_completed'
                 AND done.environment = :environment
                 {self._synthetic_clause(include_synthetic, alias="done")}
                 AND done.properties->>'match_id' = s.properties->>'match_id'
                 AND done.properties->>'termination_reason' IN ({qualifying})
                 AND done.occurred_at >= s.occurred_at
                 AND done.occurred_at < c.registered_at + make_interval(days => :window)
                GROUP BY s.subject_key
            )"""

    def _activation_sql(self, include_synthetic: bool) -> str:
        return f"""
            {self._cohort_cte(include_synthetic)},
            {self._verified_cte(include_synthetic)},
            {self._queued_cte(include_synthetic)},
            {self._started_cte(include_synthetic)},
            {self._activated_cte(include_synthetic)}
            SELECT
                (SELECT count(*) FROM cohort)    AS registered,
                (SELECT count(*) FROM verified)  AS verified,
                (SELECT count(*) FROM queued)    AS queued,
                (SELECT count(*) FROM started)   AS started,
                (SELECT count(*) FROM activated) AS activated
        """

    def _activation_params(self, environment: str, since: date, until: date) -> dict[str, Any]:
        return {
            "environment": environment,
            "since": day_start(since),
            "until": day_end(until),
            "window": ACTIVATION_WINDOW_DAYS,
        }


def _seconds(value: Any) -> float | None:
    """A percentile, as a float, or `None` for an empty sample.

    Nought would claim an instant conversion that nobody made.
    """
    return float(value) if value is not None else None
