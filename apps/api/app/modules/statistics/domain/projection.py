"""Folding one completed match into a player's record — A64-020.5F §3, §6.

Pure arithmetic over two values. No clock, no session, no I/O: what a
completed match does to a set of counters is a rule, and a rule that needs
a database to state is one nobody can check by reading it.

## Why a projection and not an aggregate

`statistics` is classified a **projection** context (DM-03): every number
is a count over match history and nothing here is the system of record for
anything. So this module answers one question — *given these counters and
this result, what are the counters now* — and the durability, the ordering
and the exactly-once guarantee belong to the layers around it.

## The watermark, and why ordering needs two fields

`current_streak` and `best_win_streak` are the only fields whose value
depends on the **order** matches are folded in, and events do not arrive
in order: the relay retries, a backfill runs beside live consumption, and
two matches can finish in the same millisecond.

So a player's row carries the total-order position of the last match
counted into it — `(counted_at, counted_match_id)` — and a match older
than that watermark updates the **counts** and leaves the streak alone.
The counts are order-independent (addition commutes); the streak is not.

Two fields rather than a timestamp alone because a timestamp alone is not
a total order: two matches completing in the same instant would compare
equal, and "is this newer" would have no answer. The match id breaks the
tie, deterministically and identically in the live consumer and the
backfill.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.modules.statistics.domain.statistics import PlayerStatistics


class MatchResultForPlayer(StrEnum):
    """What one completed match was, from one player's seat."""

    WIN = "win"
    LOSS = "loss"
    DRAW = "draw"


@dataclass(frozen=True, slots=True)
class CountedMatch:
    """One completed match, as the projection needs it.

    Deliberately not the event and not the match row: those carry a
    variant, a ply count, an engine version and two rating snapshots, none
    of which any counter here reads. A narrow input is what makes the rule
    below checkable — there is nothing in scope to accidentally depend on.
    """

    match_id: UUID
    result: MatchResultForPlayer
    completed_at: datetime

    def is_after(self, at: datetime | None, match_id: UUID | None) -> bool:
        """Whether this match is later than a watermark — §3's total order.

        `(completed_at, match_id)`, compared as a pair. A row with no
        watermark has counted nothing, so anything is later than it.

        The tie-break is not decoration: two matches finishing in the same
        millisecond is ordinary on a platform with a clock worker, and
        without the id they would compare equal and "which came last" would
        depend on arrival order — which is precisely what the watermark
        exists to stop mattering.
        """
        if at is None or match_id is None:
            return True
        return (self.completed_at, self.match_id) > (at, match_id)


@dataclass(frozen=True, slots=True)
class ProjectionState:
    """A player's counters and where they are in the total order.

    What a projection reads before folding and writes after. A value rather
    than an ORM row, so the application layer holds the *facts* and the
    mapping stays in infrastructure — which is what
    `statistics layers point inward` enforces.
    """

    statistics: PlayerStatistics
    counted_at: datetime | None
    counted_match_id: UUID | None


@dataclass(frozen=True, slots=True)
class Projected:
    """The record after one match, and whether the streak moved."""

    statistics: PlayerStatistics
    counted_at: datetime
    counted_match_id: UUID
    streak_advanced: bool
    """`False` for a match older than the watermark — its counts were
    applied and its streak contribution was not. Surfaced so an operator
    running a backfill can see how much of it arrived out of order."""


def project(current: ProjectionState, match: CountedMatch) -> Projected:
    """`current`, with one more match folded in.

    The counters always move: addition commutes, so a match arriving late
    still belongs in the totals and applying it out of order costs nothing.

    The **streak** moves only for a match later than the watermark, because
    a streak is a statement about the most recent games and folding an
    older one into it would describe a sequence that never happened.

    `current_rating` and `highest_rating` are deliberately untouched.
    They are in this contract because a profile renders them beside the
    counts, but they are `rating`'s facts — deriving them here would be a
    second, competing answer to what a player rates (§3).
    """
    advanced = match.is_after(current.counted_at, current.counted_match_id)
    counters = current.statistics

    wins = counters.wins + (1 if match.result is MatchResultForPlayer.WIN else 0)
    losses = counters.losses + (1 if match.result is MatchResultForPlayer.LOSS else 0)
    draws = counters.draws + (1 if match.result is MatchResultForPlayer.DRAW else 0)

    streak = (
        _streak_after(counters.current_streak, match.result)
        if advanced
        else counters.current_streak
    )
    best = max(counters.best_win_streak, streak)

    return Projected(
        statistics=replace(
            counters,
            games_played=counters.games_played + 1,
            wins=wins,
            losses=losses,
            draws=draws,
            current_streak=streak,
            best_win_streak=best,
        ),
        # The watermark only moves forward. A late match leaves it where it
        # was, so the next in-order match still compares against the real
        # high-water mark rather than against something behind it.
        counted_at=(match.completed_at if advanced else (current.counted_at or match.completed_at)),
        counted_match_id=(
            match.match_id if advanced else (current.counted_match_id or match.match_id)
        ),
        streak_advanced=advanced,
    )


def _streak_after(streak: int, result: MatchResultForPlayer) -> int:
    """The run after one more result.

    Signed, as `PlayerStatistics.current_streak` defines it: positive
    counts consecutive wins, negative consecutive losses, zero means the
    last match was a draw. A win after a loss restarts at `+1` rather than
    continuing — which is why this is `max`/`min` against 0 and not a plain
    increment.
    """
    if result is MatchResultForPlayer.DRAW:
        return 0
    if result is MatchResultForPlayer.WIN:
        return max(streak, 0) + 1
    return min(streak, 0) - 1


__all__ = [
    "CountedMatch",
    "MatchResultForPlayer",
    "Projected",
    "ProjectionState",
    "project",
]
