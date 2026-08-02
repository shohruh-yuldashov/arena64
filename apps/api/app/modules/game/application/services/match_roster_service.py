"""`GameMatchRoster` — `game`'s side of the gateway's membership check.
A64-016.2 §7.

Eight lines of body, and that is the whole point: the question "is this
player in this match" is answered by a primary-key read and a projection, so
there is nothing here for a service to get wrong. What it exists for is the
**boundary** — the gateway holds `MatchRosterReader` and therefore cannot
reach a `MatchRecord`, a repository, or any other capability on this module.

Beside `GamePairingSettlements` and `GameRecentOpponents`, which are the same
shape for the same reason: a published read, implemented over the repository
`game` already has, named for the question its consumer asks rather than for
the table it reads.
"""

import logging
from uuid import UUID

from app.modules.game.application.ports import MatchRecordRepository
from app.modules.game.public.rooms import MatchRoster

logger = logging.getLogger(__name__)


class GameMatchRoster:
    """`MatchRosterReader` over the match relation."""

    def __init__(self, matches: MatchRecordRepository) -> None:
        self._matches = matches

    async def roster_of(self, match_id: UUID) -> MatchRoster | None:
        """The two players in a match, or `None`.

        No lock and no status filter. The **status is published rather than
        applied** because whether a room may open for a `pending_acceptance`
        match is the gateway's rule to hold — see `MatchRoster.status`, and
        `GameRoomService` for the rule as it stands today. A reader that
        filtered here would make that decision unreachable and would have to
        be changed the first time it moved.
        """
        record = await self._matches.by_id(match_id)
        if record is None:
            return None

        return MatchRoster(
            match_id=record.id,
            light_player_id=record.light.player_id,
            dark_player_id=record.dark.player_id,
            status=record.status,
        )


__all__ = ["GameMatchRoster"]
