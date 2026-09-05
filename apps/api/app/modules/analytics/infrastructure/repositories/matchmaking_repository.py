"""Matchmaking and game health queries — A64-027.5.

Set-based, and every count carries its grain in the SQL rather than in a
comment:

    queue attempts     `COUNT(*)` over ticket-grain events
    offers             `COUNT(*)` over `match_offer_resolved`, one row per
                       pairing
    matches            `COUNT(DISTINCT properties->>'match_id')` over
                       `match_started`, which is projected per seat

That last one is the difference between a completion rate and half a
completion rate, and it is done in SQL rather than by dividing by two —
a match with one projected seat would then be half a match.

## Segmentation

Every query takes an optional `rated` and `speed_class`, and applies them
to **both halves of every rate**. §34: a "rated acceptance rate" whose
numerator is rated offers and whose denominator is all offers is a number
that means nothing, and it looks like a percentage.

The filter is applied inside each CTE rather than to the result, so a
segmented denominator is a segmented population and not a filtered answer.
"""

from datetime import date
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.domain.properties import TerminationReason
from app.modules.analytics.infrastructure.repositories.funnel_repository import (
    NON_QUALIFYING,
    day_end,
    day_start,
)

#: §32, and the same list the funnels use — imported rather than repeated,
#: because two copies of a completion classification is two answers to
#: "did this game happen".
QUALIFYING: Final = tuple(
    sorted(reason.value for reason in TerminationReason if reason.value not in NON_QUALIFYING)
)

_QUALIFYING_SQL: Final = ", ".join(f"'{reason}'" for reason in QUALIFYING)
_ABORT_SQL: Final = ", ".join(f"'{reason}'" for reason in sorted(NON_QUALIFYING))


class SqlAlchemyMatchmakingReader:
    """Queue, offer and game health over one window."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def queue_health(
        self,
        *,
        environment: str,
        since: date,
        until: date,
        include_synthetic: bool,
        rated: bool | None = None,
    ) -> dict[str, Any]:
        """M6, M7, M7b and M8, in one statement.

        The wait percentiles run over **distinct pairings**: `match_found`
        is projected per seat and both seats carry the pair's own wait, so
        a percentile over rows would be correct and a sample size over rows
        would be double. The inner `DISTINCT` fixes the second without
        changing the first.
        """
        statement = text(f"""
            WITH scoped AS (
                SELECT event_name, properties
                FROM analytics.event
                WHERE environment = :environment
                  {self._synthetic(include_synthetic)}
                  AND occurred_at >= :since AND occurred_at < :until
                  {self._rated(rated)}
            ),
            pairings AS (
                SELECT DISTINCT
                    properties->>'match_id' AS match_id,
                    (properties->>'waited_ms')::bigint AS waited_ms
                FROM scoped
                WHERE event_name = 'match_found'
            )
            SELECT
                (SELECT count(*) FROM scoped WHERE event_name = 'queue_joined') AS joins,
                (SELECT count(*) FROM scoped WHERE event_name = 'match_found') AS paired,
                (SELECT count(*) FROM scoped WHERE event_name = 'queue_left') AS abandoned,
                (SELECT count(*) FROM scoped
                  WHERE event_name = 'queue_left'
                    AND properties->>'reason' = 'cancelled') AS cancelled,
                (SELECT count(*) FROM scoped
                  WHERE event_name = 'queue_left'
                    AND properties->>'reason' = 'expired') AS expired,
                (SELECT count(*) FROM pairings) AS wait_sample,
                (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY waited_ms)
                   FROM pairings) AS wait_p50,
                (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY waited_ms)
                   FROM pairings) AS wait_p95
        """)
        row = (
            await self._session.execute(statement, self._params(environment, since, until))
        ).one()
        return {
            "joins": row.joins,
            "paired": row.paired,
            "abandoned": row.abandoned,
            "cancelled": row.cancelled,
            "expired": row.expired,
            "wait_sample": row.wait_sample,
            "wait_p50": _seconds(row.wait_p50),
            "wait_p95": _seconds(row.wait_p95),
        }

    async def offer_health(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> dict[str, int]:
        """M9. One row per offer, three exhaustive outcomes."""
        statement = text(f"""
            SELECT
                count(*) FILTER (WHERE properties->>'resolution' = 'both_accepted') AS accepted,
                count(*) FILTER (WHERE properties->>'resolution' = 'declined') AS declined,
                count(*) FILTER (WHERE properties->>'resolution' = 'expired') AS expired
            FROM analytics.event
            WHERE event_name = 'match_offer_resolved'
              AND environment = :environment
              {self._synthetic(include_synthetic)}
              AND occurred_at >= :since AND occurred_at < :until
        """)
        row = (
            await self._session.execute(statement, self._params(environment, since, until))
        ).one()
        return {"accepted": row.accepted, "declined": row.declined, "expired": row.expired}

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
        """M10 – M14, at **match grain**.

        `started` counts distinct `match_id` over `match_started`, which is
        projected per seat. Counting rows would report every match twice
        and halve every rate — a defect that produces a plausible-looking
        percentage, which is why it is a `DISTINCT` and not a division.
        """
        statement = text(f"""
            WITH scoped AS (
                SELECT event_name, properties
                FROM analytics.event
                WHERE environment = :environment
                  {self._synthetic(include_synthetic)}
                  AND occurred_at >= :since AND occurred_at < :until
                  {self._rated(rated)}
                  {self._speed(speed_class)}
            ),
            started AS (
                SELECT DISTINCT properties->>'match_id' AS match_id
                FROM scoped WHERE event_name = 'match_started'
            ),
            finished AS (
                SELECT properties->>'match_id' AS match_id,
                       properties->>'termination_reason' AS reason,
                       properties->>'outcome' AS outcome,
                       (properties->>'rated')::boolean AS rated
                FROM scoped WHERE event_name = 'match_completed'
            )
            SELECT
                (SELECT count(*) FROM started) AS started,
                (SELECT count(*) FROM finished
                  WHERE reason IN ({_QUALIFYING_SQL})) AS completed,
                (SELECT count(*) FROM finished
                  WHERE reason IN ({_ABORT_SQL})) AS aborted,
                (SELECT count(*) FROM finished WHERE reason = 'resignation') AS resignations,
                (SELECT count(*) FROM finished
                  WHERE outcome = 'draw' AND reason IN ({_QUALIFYING_SQL})) AS draws,
                (SELECT count(*) FROM finished WHERE reason = 'abandonment') AS abandonments,
                (SELECT count(*) FROM finished WHERE reason = 'flag') AS flags,
                (SELECT count(*) FROM finished
                  WHERE rated AND reason IN ({_QUALIFYING_SQL})) AS rated_completions
        """)
        row = (
            await self._session.execute(statement, self._params(environment, since, until))
        ).one()

        breakdown = (
            await self._session.execute(
                text(f"""
                    SELECT properties->>'termination_reason' AS reason, count(*) AS total
                    FROM analytics.event
                    WHERE event_name = 'match_completed'
                      AND environment = :environment
                      {self._synthetic(include_synthetic)}
                      AND occurred_at >= :since AND occurred_at < :until
                      {self._rated(rated)}
                      {self._speed(speed_class)}
                    GROUP BY reason
                    ORDER BY total DESC, reason
                """),
                self._params(environment, since, until),
            )
        ).all()

        return {
            "started": row.started,
            "completed": row.completed,
            "aborted": row.aborted,
            "resignations": row.resignations,
            "draws": row.draws,
            "abandonments": row.abandonments,
            "flags": row.flags,
            "rated_completions": row.rated_completions,
            "breakdown": tuple((item.reason, item.total) for item in breakdown),
        }

    async def data_quality(
        self, *, environment: str, since: date, until: date, include_synthetic: bool
    ) -> dict[str, int]:
        """Three impossibilities, counted rather than repaired — §48.

        Raw events are never rewritten. Each of these is a lifecycle the
        platform should not have produced, and a rising count is a contract
        problem upstream rather than a query bug here.
        """
        statement = text(f"""
            WITH scoped AS (
                SELECT event_name, properties, occurred_at
                FROM analytics.event
                WHERE environment = :environment
                  {self._synthetic(include_synthetic)}
                  AND occurred_at >= :since AND occurred_at < :until
            )
            SELECT
                (SELECT count(*) FROM scoped done
                  WHERE done.event_name = 'match_completed'
                    AND NOT EXISTS (
                        SELECT 1 FROM scoped s
                        WHERE s.event_name = 'match_started'
                          AND s.properties->>'match_id' = done.properties->>'match_id'
                    )) AS completions_without_start,
                (SELECT count(*) FROM scoped done
                  JOIN scoped s
                    ON s.event_name = 'match_started'
                   AND s.properties->>'match_id' = done.properties->>'match_id'
                  WHERE done.event_name = 'match_completed'
                    AND done.occurred_at < s.occurred_at) AS completed_before_start,
                (SELECT count(*) FROM scoped
                  WHERE event_name = 'match_offer_resolved'
                    AND properties->>'resolution' NOT IN
                        ('both_accepted', 'declined', 'expired')) AS unknown_resolutions
        """)
        row = (
            await self._session.execute(statement, self._params(environment, since, until))
        ).one()
        return {
            "completions_without_start": row.completions_without_start,
            "completed_before_start": row.completed_before_start,
            "unknown_resolutions": row.unknown_resolutions,
        }

    # --- filters ----------------------------------------------------------

    def _synthetic(self, include_synthetic: bool, *, alias: str = "") -> str:
        if include_synthetic:
            return ""
        prefix = f"{alias}." if alias else ""
        return f"AND {prefix}is_synthetic = false"

    def _rated(self, rated: bool | None) -> str:
        """§34. Applied inside the CTE, so a segmented denominator is a
        segmented population rather than a filtered answer."""
        if rated is None:
            return ""
        return f"AND properties->>'rated' = '{str(rated).lower()}'"

    def _speed(self, speed_class: str | None) -> str:
        if speed_class is None:
            return ""
        # A member of the analytics vocabulary, not caller text: the
        # service validates it against the enum before this sees it.
        return f"AND properties->>'speed_class' = '{speed_class}'"

    def _params(self, environment: str, since: date, until: date) -> dict[str, Any]:
        return {
            "environment": environment,
            "since": day_start(since),
            "until": day_end(until),
        }


def _seconds(milliseconds: Any) -> float | None:
    """Milliseconds on the wire, **seconds** in every result — the
    platform-wide unit `MetricsRecorder` already requires."""
    return float(milliseconds) / 1000 if milliseconds is not None else None
