"""`GameRecentOpponents` — QT-3's rematch guard, over the match table.

Implements `game.public.RecentOpponentReader`, which A64-015.3 declared and
could not satisfy because `game` had no durable match. That port's
docstring records what "recent" means today and why the current definition
is deliberately wider than QT-3's; nothing in this file re-argues it.

## One query, whatever the batch

The scan hands over a whole pool — two hundred candidates at the default
`MATCHMAKING_CANDIDATE_BATCH_SIZE` — and gets one statement back. A
per-candidate read would be the N+1 CLAUDE.md §10.4 names, and it would sit
inside a job that runs several times a second per pool, which is the worst
place on the platform to put one.

## Never raises, and that is a rule rather than caution

An unreadable match history degrades to "no exclusions". A rematch is a
disappointment; a pairing scan that stops because a read failed is an empty
queue, which is an outage. The port says so, and this is where it is true.
"""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.game.application.ports import MatchRecordRepository

logger = logging.getLogger(__name__)


class GameRecentOpponents:
    """The recent-opponent read, over one session."""

    def __init__(self, matches: MatchRecordRepository) -> None:
        self._matches = matches

    async def recent_opponents_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        """Each player's most recent opponent, where that opponent is also
        in the batch.

        Restricted to the batch because the caller is deciding which of
        *these* tickets may meet: an opponent who is not in the pool is not
        an exclusion, only a row nobody will compare against.

        The result is one-directional — the player who played is the key —
        and that is safe because `PairExclusions.forbids` checks both
        directions. Recording it symmetrically here would be a second place
        the same fact is stored.
        """
        batch = set(player_ids)
        if len(batch) < 2:
            # One candidate cannot be excluded from anybody, and a pool of
            # one is the common shape of a quiet queue. Returning early
            # keeps the query off the hot path for the case where it could
            # not change the answer.
            return {}

        try:
            latest = await self._matches.latest_opponent_among(sorted(batch))
        except Exception as error:  # noqa: BLE001 — an unreadable history must not stop pairing
            logger.error(
                "recent_opponents_unavailable",
                extra={"batch": len(batch), "error": type(error).__name__},
                exc_info=error,
            )
            return {}

        return {
            player_id: frozenset({opponent_id})
            for player_id, opponent_id in latest.items()
            if opponent_id in batch
        }


__all__ = ["GameRecentOpponents"]
