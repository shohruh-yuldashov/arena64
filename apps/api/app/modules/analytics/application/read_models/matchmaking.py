"""Matchmaking and game health results — A64-027.5.

Parts III and IV's conventions apply unchanged. What this file adds is a
**grain** on every type, because these metrics do not share one:

    QueueHealth       queue attempt — one ticket, one unit
    OfferHealth       offer — one pairing's acceptance decision
    GameHealth        match — one game, never one seat

Mixing them is the failure §40 of the task names, and it is invisible in a
result: a completion rate over player facts is exactly twice a completion
rate over matches, and both look like plausible percentages.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from app.modules.analytics.application.read_models.funnels import FunnelMeta


class Grain(StrEnum):
    """What one unit of a metric is.

    Declared on every result rather than left to documentation, so a caller
    combining two of them has to notice they are counting different things.
    """

    #: One queue ticket. A player who joins, leaves and joins again
    #: produced two — matchmaking measures attempts, unlike DAU.
    QUEUE_ATTEMPT = "queue_attempt"

    #: One pairing's acceptance outcome. Not one per player: the two seats
    #: share a single resolution.
    OFFER = "offer"

    #: One game. Never one seat — a two-player match is one match.
    MATCH = "match"


@dataclass(frozen=True, slots=True)
class WaitDistribution:
    """Queue wait, in **seconds**, over successful pairings — M7.

    `sample` counts **pairings**, not seats. `match_found` is projected per
    seat and both carry the pair's own wait, so counting rows would report
    twice the pairings that happened. The percentiles are unaffected by
    that duplication — doubling every value uniformly leaves quantiles
    where they were — but a sample size that is twice the truth is a
    number somebody will divide by.

    **Successful pairings only.** A64-027.1 §29 says so, and M7b is the
    honest companion: without it a p95 flatters a product whose slow waits
    end in people giving up rather than in a pairing.
    """

    sample: int
    p50_seconds: float | None
    p95_seconds: float | None


@dataclass(frozen=True, slots=True)
class QueueHealth:
    """M6, M7, M7b and M8 — one window, one grain.

    Every count here is a **queue attempt**. `paired_attempts` counts
    `match_found` seats, and a seat *is* a ticket consumed by a pairing, so
    the two halves of M7b and M8 are consistent by construction rather than
    by a conversion factor somebody has to remember.
    """

    grain: Grain
    period_start: date
    period_end: date
    meta: FunnelMeta

    #: M6.
    queue_joins: int

    #: Tickets a pairing consumed — `match_found` seats.
    paired_attempts: int

    #: M7b's numerator, split by why the wait ended.
    abandoned_attempts: int
    cancelled_attempts: int
    expired_attempts: int

    #: M7.
    wait: WaitDistribution

    @property
    def abandonment_rate(self) -> float | None:
        """M7b, exactly: `queue_left / (queue_left + match_found)`.

        The denominator is **resolved** attempts, not joins: a ticket still
        waiting when the window closed has not abandoned anything yet, and
        counting it as a failure would make a busy minute look like an
        outage.
        """
        resolved = self.abandoned_attempts + self.paired_attempts
        if resolved <= 0:
            return None
        return self.abandoned_attempts / resolved

    @property
    def match_found_rate(self) -> float | None:
        """M8: `distinct match_found seats / queue_joined`.

        A different denominator from M7b's on purpose — this one asks what
        share of *joins* produced a pairing, and A64-027.1 notes the
        limitation it carries: a ticket spanning midnight is counted in the
        day it joined.
        """
        if self.queue_joins <= 0:
            return None
        return self.paired_attempts / self.queue_joins


@dataclass(frozen=True, slots=True)
class OfferHealth:
    """M9 — offers, and how they ended.

    Grain: one offer, which is one pairing. The three outcomes are
    exhaustive over resolved offers, and `resolved` is asserted equal to
    their sum by a test — a breakdown that does not add up is a category
    somebody forgot to project.
    """

    grain: Grain
    period_start: date
    period_end: date
    meta: FunnelMeta

    accepted: int
    declined: int
    expired: int

    @property
    def resolved(self) -> int:
        return self.accepted + self.declined + self.expired

    @property
    def acceptance_rate(self) -> float | None:
        """M9: `both_accepted / all match_offer_resolved`.

        **Expiry stays in the denominator.** A64-027.1's formula says "all
        resolved offers", and dropping the ones nobody answered would turn
        a product where half the offers time out into one with a perfect
        acceptance rate.
        """
        if self.resolved <= 0:
            return None
        return self.accepted / self.resolved


@dataclass(frozen=True, slots=True)
class GameHealth:
    """M10 – M14 — one game per unit.

    `match_completed` is entity-level, so a completion is already one row
    per match. `match_started` is per seat, so the started count divides by
    two — done in SQL over `DISTINCT match_id`, not by arithmetic, because
    a match with one projected seat would then be half a match.
    """

    grain: Grain
    period_start: date
    period_end: date
    meta: FunnelMeta

    #: Distinct matches with a start row.
    started: int

    #: Completions excluding aborts — §32's numerator and denominator both
    #: exclude them, so this is the population every rate below is over.
    completed: int

    #: Excluded from both sides of M10. Reported because "how many games
    #: never happened" is a product question of its own.
    aborted: int

    resignations: int
    draws: int
    abandonments: int
    flags: int
    rated_completions: int

    #: Every canonical reason with a non-zero count, for M11's neighbours.
    #: A closed vocabulary — never a free string.
    termination_breakdown: tuple[tuple[str, int], ...]

    @property
    def completion_rate(self) -> float | None:
        """M10, per §32: completions over starts, **aborts excluded from
        both sides**."""
        eligible = self.started - self.aborted
        if eligible <= 0:
            return None
        return self.completed / eligible

    @property
    def resignation_rate(self) -> float | None:
        """M11. A resignation is a completed game — §32 — so the
        denominator is completions and not starts."""
        return _over(self.resignations, self.completed)

    @property
    def draw_rate(self) -> float | None:
        return _over(self.draws, self.completed)

    @property
    def abandonment_rate(self) -> float | None:
        """M13: `abandonment` and `flag` together.

        Reported alongside `flags` so the two can be separated: A64-027.1
        notes that losing on time is a legitimate result and walking away
        is not, even though both end a game without a move.
        """
        return _over(self.abandonments + self.flags, self.completed)

    @property
    def rated_share(self) -> float | None:
        return _over(self.rated_completions, self.completed)


def _over(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return part / whole
