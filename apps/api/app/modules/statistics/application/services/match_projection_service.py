"""`MatchProjectionService` — one completed match, counted once per player.
A64-020.5F §3, §5, §6.

    claim the match for the player  ->  lock the row  ->  fold  ->  write

One transaction for all four, and the order is the guarantee: the claim is
an `ON CONFLICT DO NOTHING` insert into `processed_match`, so a second
delivery of the same match for the same player finds the row already there
and does nothing. There is no commit between the claim and the counters, so
a crash cannot leave one without the other.

## Why the marker is the match and the player, not the event

§5, and the reason is the backfill. The platform's `processed_event` ledger
is keyed by outbox event id — which the live path has and a backfill does
not. Keying on `(match_id, player_id)` gives both paths the **same**
identity, so a match counted live and the same match reached by a backfill
collide on the primary key and the second is refused. Nothing else makes
"backfill overlapping with live consumption" safe.

## One transaction per match, owned here

`SessionScopedNotificationHandler` opens a session per *batch* and does not
commit — the service owns the transaction, exactly as `MatchRatingService`
does for ratings. So this commits once per match, and that is the right
granularity for both callers: a relay batch of ten completions produces ten
independent durable results, and a backfill can be stopped between any two
matches without losing the one in flight.

## Both players in one transaction, and why that is a choice

A match produces two projections and they are written together, so a reader
never sees a game credited to one player and not the other. The cost is a
lock on two rows for the length of one transaction, which is two primary-key
row locks held for two updates — and the alternative is a window in which
the platform's own totals disagree with themselves.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.statistics.application.ports import StatisticsProjectionRepository
from app.modules.statistics.domain.projection import (
    CountedMatch,
    MatchResultForPlayer,
    project,
)

logger = logging.getLogger(__name__)


class ProjectionOutcome(StrEnum):
    """What one match did to the projection — §8's bounded outcome."""

    APPLIED = "applied"
    ALREADY_PROCESSED = "already_processed"
    """Both players were already credited. The ordinary answer to a retry,
    a redelivery, or a backfill passing over live-counted history."""

    IGNORED_NON_COUNTING = "ignored_non_counting"
    """A match that was not played to a result — an abort. MT-11 keeps it
    out of every rating and statistic, so it is skipped rather than counted
    as a draw."""

    REJECTED_INVALID = "rejected_invalid"
    """The facts needed to count it are not there. Never guessed: a
    fabricated seat would put a game on somebody's record that they did not
    play."""


@dataclass(frozen=True, slots=True)
class CompletedMatchFacts:
    """Exactly what the projection needs about one finished match.

    Narrow on purpose, and shared by both callers: the consumer builds one
    from an event payload and the backfill builds one from a match row, so
    the *rules* below cannot diverge between live and historical counting
    even though the inputs do.
    """

    match_id: UUID
    light_player_id: UUID
    dark_player_id: UUID
    outcome: str
    winner: str | None
    completed_at: datetime


class MatchProjectionService:
    """Folds completed matches into player statistics."""

    def __init__(
        self,
        *,
        statistics: StatisticsProjectionRepository,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._statistics = statistics
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def apply(self, facts: CompletedMatchFacts) -> ProjectionOutcome:
        """Counts one match for both players, in one transaction.

        Both claims and both counter updates commit together, so a reader
        never sees a game credited to one player and not the other — and a
        failure anywhere rolls back the claims with the counts, which is
        what makes a retry find nothing already marked.

        Returns `ALREADY_PROCESSED` only when *neither* player was claimed.
        """
        results = _results_for(facts)
        if results is None:
            return ProjectionOutcome.IGNORED_NON_COUNTING

        async with self._unit_of_work:
            applied = False
            for player_id, result in results:
                if await self._count(facts, player_id, result):
                    applied = True
            await self._unit_of_work.commit()

        return ProjectionOutcome.APPLIED if applied else ProjectionOutcome.ALREADY_PROCESSED

    async def _count(
        self, facts: CompletedMatchFacts, player_id: UUID, result: MatchResultForPlayer
    ) -> bool:
        """One player's share. `False` if they were already credited.

        The claim is **first**: if it does not insert, this player has this
        match already and nothing else runs. That is what makes a duplicate
        delivery cost one failed insert rather than a second increment.
        """
        if not await self._statistics.claim(facts.match_id, player_id, at=self._clock.now()):
            return False

        state = await self._statistics.state_for_update(player_id)
        projected = project(
            state,
            CountedMatch(match_id=facts.match_id, result=result, completed_at=facts.completed_at),
        )
        await self._statistics.write(player_id, projected)

        if not projected.streak_advanced:
            # Counted, but out of order — its streak contribution was
            # dropped. Logged rather than failed: the totals are right and
            # the streak reflects the most recent games, which is what a
            # streak is for. A rising rate here during live consumption
            # would mean events are arriving badly out of order.
            logger.info(
                "statistics_match_counted_late",
                extra={"match_id": str(facts.match_id)},
            )
        return True


def _results_for(
    facts: CompletedMatchFacts,
) -> list[tuple[UUID, MatchResultForPlayer]] | None:
    """Each seat's result, or `None` for a match that does not count.

    **`none` is not a draw.** `MatchOutcome.NONE` is an aborted match —
    MT-11's "a match that did not happen" — and counting it as a draw would
    put a game on two records that neither player played. Every other
    outcome counts according to its result, whatever ended it: a
    resignation, a flag, an abandonment and a checkmate are all games that
    happened.

    An unrecognised outcome returns `None` rather than a guess. A value this
    build does not know is a contract change, and inventing a meaning for it
    would write it into a permanent record.
    """
    if facts.outcome == "draw":
        return [
            (facts.light_player_id, MatchResultForPlayer.DRAW),
            (facts.dark_player_id, MatchResultForPlayer.DRAW),
        ]

    if facts.outcome != "win" or facts.winner not in ("light", "dark"):
        return None

    winner_id = facts.light_player_id if facts.winner == "light" else facts.dark_player_id
    loser_id = facts.dark_player_id if facts.winner == "light" else facts.light_player_id
    return [
        (winner_id, MatchResultForPlayer.WIN),
        (loser_id, MatchResultForPlayer.LOSS),
    ]


__all__ = [
    "CompletedMatchFacts",
    "MatchProjectionService",
    "ProjectionOutcome",
]
