"""Engagement and retention queries — A64-027.4.

Set-based, like the funnels, and built on the same two ideas: a cohort CTE
of one row per registered subject, and every count over **distinct
subjects** rather than over rows.

## The activity predicate is one list, in one place

A64-027.1 §30 defines an active player as somebody who started a match,
entered a tournament or sent a challenge. That list appears **once**, in
`ACTIVITY_EVENTS`, and every query below interpolates it. Three copies would
be three places for DAU and retention to disagree about what activity is —
and they would disagree silently, because both numbers would still look
plausible.

## Retention is a calendar-day question

A64-027.1 §33 froze D1/D7/D30 as *that* calendar day in UTC, not "within
that many days". So each column is a join against a one-day range offset
from the cohort day, and a cohort whose target day has not arrived reports
`NULL` rather than nought — the difference between "nobody came back" and
"we have not looked yet".
"""

from datetime import date, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.infrastructure.repositories.funnel_repository import (
    day_end,
    day_start,
)

#: A64-027.1 §30, and the only place this list exists.
ACTIVITY_EVENTS: Final = ("match_started", "tournament_entered", "challenge_sent")

_ACTIVITY_SQL: Final = ", ".join(f"'{name}'" for name in ACTIVITY_EVENTS)

#: The three retention offsets §33 names. Days, from the cohort day.
RETENTION_DAYS_OFFSETS: Final = (1, 7, 30)


class SqlAlchemyEngagementReader:
    """Active players, retention cohorts and the weekly engagement metrics."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- active players ---------------------------------------------------

    async def active_players(
        self, *, environment: str, as_of: date, include_synthetic: bool
    ) -> dict[str, int]:
        """DAU, WAU and MAU, all ending on `as_of`.

        One statement rather than three, so the three windows are measured
        against the same snapshot — running them separately would let an
        event land between two of them and make `daily / monthly` exceed 1
        on a busy boundary.

        The windows are **inclusive of `as_of`** and look backwards: DAU is
        that day, WAU is the seven days ending on it, MAU the thirty.
        """
        statement = text(f"""
            SELECT
                count(DISTINCT subject_key) FILTER (WHERE occurred_at >= :day) AS daily,
                count(DISTINCT subject_key) FILTER (WHERE occurred_at >= :week) AS weekly,
                count(DISTINCT subject_key) AS monthly
            FROM analytics.event
            WHERE event_name IN ({_ACTIVITY_SQL})
              AND environment = :environment
              {self._synthetic(include_synthetic)}
              AND subject_key IS NOT NULL
              AND occurred_at >= :month AND occurred_at < :until
        """)
        row = (
            await self._session.execute(
                statement,
                {
                    "environment": environment,
                    "day": day_start(as_of),
                    "week": day_start(as_of - timedelta(days=6)),
                    "month": day_start(as_of - timedelta(days=29)),
                    "until": day_end(as_of),
                },
            )
        ).one()
        return {"daily": row.daily, "weekly": row.weekly, "monthly": row.monthly}

    # --- retention --------------------------------------------------------

    async def retention(
        self, *, environment: str, since: date, until: date, include_synthetic: bool, today: date
    ) -> list[dict[str, Any]]:
        """One row per registration cohort day, with D1, D7 and D30.

        Each retention column is a `LEFT JOIN LATERAL` over that cohort's
        own subjects, looking at exactly one calendar day. `NULL` where the
        target day has not arrived yet — computed in SQL against `:today`
        rather than filtered afterwards, so a partial cohort is never a zero
        that a caller has to remember to ignore.
        """
        columns = []
        joins = []
        for offset in RETENTION_DAYS_OFFSETS:
            columns.append(
                f"CASE WHEN c.cohort_day + {offset} <= :today "
                f"THEN count(DISTINCT r{offset}.subject_key) ELSE NULL END AS d{offset}"
            )
            joins.append(f"""
                LEFT JOIN analytics.event r{offset}
                  ON r{offset}.subject_key = c.subject_key
                 AND r{offset}.event_name IN ({_ACTIVITY_SQL})
                 AND r{offset}.environment = :environment
                 {self._synthetic(include_synthetic, alias=f"r{offset}")}
                 AND r{offset}.occurred_at >= (c.cohort_day + {offset})::timestamptz
                 AND r{offset}.occurred_at < (c.cohort_day + {offset} + 1)::timestamptz""")

        statement = text(f"""
            WITH cohort AS (
                SELECT
                    subject_key,
                    (MIN(occurred_at) AT TIME ZONE 'UTC')::date AS cohort_day
                FROM analytics.event
                WHERE event_name = 'user_registered'
                  AND environment = :environment
                  {self._synthetic(include_synthetic)}
                  AND subject_key IS NOT NULL
                  AND occurred_at >= :since AND occurred_at < :until
                GROUP BY subject_key
            )
            SELECT
                c.cohort_day,
                count(DISTINCT c.subject_key) AS cohort,
                {", ".join(columns)}
            FROM cohort c
            {"".join(joins)}
            GROUP BY c.cohort_day
            ORDER BY c.cohort_day
        """)
        rows = (
            (
                await self._session.execute(
                    statement,
                    {
                        "environment": environment,
                        "since": day_start(since),
                        "until": day_end(until),
                        "today": today,
                    },
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    # --- weekly engagement ------------------------------------------------

    async def engagement(
        self, *, environment: str, week_start: date, week_end: date, include_synthetic: bool
    ) -> dict[str, Any]:
        """M15, M16, M17 and M22 over one week, in one statement.

        One query rather than four, because every one of them shares the
        same window and the same active-player denominator — four
        statements would let three dashboards disagree about which week
        they meant.

        `median_matches` is a percentile over **per-player counts**, which
        is why the inner aggregate exists: A64-027.1 §29 asks for the median
        beside the mean because a handful of people play a great deal and a
        mean over that describes nobody.
        """
        statement = text(f"""
            WITH window_events AS (
                SELECT subject_key, event_name, properties
                FROM analytics.event
                WHERE environment = :environment
                  {self._synthetic(include_synthetic)}
                  AND occurred_at >= :since AND occurred_at < :until
            ),
            active AS (
                SELECT DISTINCT subject_key FROM window_events
                WHERE event_name IN ({_ACTIVITY_SQL}) AND subject_key IS NOT NULL
            ),
            per_player_matches AS (
                SELECT subject_key, count(*) AS matches
                FROM window_events
                WHERE event_name = 'match_started' AND subject_key IS NOT NULL
                GROUP BY subject_key
            )
            SELECT
                (SELECT count(*) FROM active) AS active_players,
                (SELECT coalesce(sum(matches), 0) FROM per_player_matches) AS match_starts,
                (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY matches)
                   FROM per_player_matches) AS median_matches,
                (SELECT count(DISTINCT subject_key) FROM window_events
                  WHERE event_name = 'tournament_entered') AS tournament_entrants,
                (SELECT count(*) FROM window_events
                  WHERE event_name = 'friendship_created') AS friendships,
                (SELECT count(*) FROM window_events
                  WHERE event_name = 'challenge_sent') AS challenges_sent,
                (SELECT count(*) FROM window_events
                  WHERE event_name = 'challenge_resolved'
                    AND properties->>'resolution' = 'accepted') AS accepted,
                (SELECT count(*) FROM window_events
                  WHERE event_name = 'challenge_resolved'
                    AND properties->>'resolution' = 'declined') AS declined,
                (SELECT count(*) FROM window_events
                  WHERE event_name = 'challenge_resolved'
                    AND properties->>'resolution' = 'expired') AS expired,
                (SELECT count(*) FROM window_events
                  WHERE event_name = 'challenge_resolved'
                    AND properties->>'resolution' = 'cancelled') AS cancelled
        """)
        row = (
            await self._session.execute(
                statement,
                {
                    "environment": environment,
                    "since": day_start(week_start),
                    "until": day_end(week_end),
                },
            )
        ).one()
        return {
            "active_players": row.active_players,
            "match_starts": row.match_starts,
            "median_matches": float(row.median_matches) if row.median_matches is not None else None,
            "tournament_entrants": row.tournament_entrants,
            "friendships": row.friendships,
            "challenges_sent": row.challenges_sent,
            "accepted": row.accepted,
            "declined": row.declined,
            "expired": row.expired,
            "cancelled": row.cancelled,
        }

    def _synthetic(self, include_synthetic: bool, *, alias: str = "") -> str:
        """Excluded by default, in every CTE — the rule A64-027.3's mutation
        check established: a filter only on the outermost query passes a
        test suite and still lets a synthetic player's later events through.
        """
        if include_synthetic:
            return ""
        prefix = f"{alias}." if alias else ""
        return f"AND {prefix}is_synthetic = false"
