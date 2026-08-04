"""`PersistedMatchReplay` — loading a stored match into `ReplayData`.
A64-016.4 §4.

The smallest adapter that lets `ReplayEngine` consume the durable log, and
deliberately nothing more. §4 is explicit: "Do not create a second replay
format." There is one — `ReplayData` — and this builds it.

## Why it is this small

Because the log was designed to be replay-compatible rather than translated
into compatibility. `MoveLogRepository.for_replay` already returns
`MoveRecord`s, which is what `ReplayData` holds; the match row already
carries the variant and the engine version; and the opening position is
derived from the variant, deterministically, which is why §4 says the
occurrence counts must **not** be persisted — replay rebuilds them by
applying the log.

So the whole adapter is: read two things, name them.

## What it is for

Two callers, one of which does not exist yet:

    `LiveMoveService`   rebuilds the aggregate on every submission, which
                        is what makes the durable log authoritative rather
                        than a write-behind copy of Redis
    verification        replaying a finished game and checking it reaches
                        the recorded result. Nothing does this yet; it is
                        the reason `expected_result` is filled

`expected_result` is populated for a completed match precisely so a replay
**fails** when the log and the result disagree. A replay that could not
disagree would prove nothing.
"""

import logging
from uuid import UUID

from app.modules.engine import PlayerSide, Position, initial_board
from app.modules.game.application.ports import MatchRecordRepository, MoveLogRepository
from app.modules.game.domain.replay import ReplayData
from app.modules.game.domain.variants import board_variant_of

logger = logging.getLogger(__name__)


class PersistedMatchReplay:
    """Builds `ReplayData` from a stored match and its move log."""

    def __init__(self, *, matches: MatchRecordRepository, moves: MoveLogRepository) -> None:
        self._matches = matches
        self._moves = moves

    async def replay_data(self, match_id: UUID) -> ReplayData | None:
        """Everything needed to replay one match, or `None` if unknown.

        The opening position is **derived from the variant**, not stored.
        Deterministic — `initial_board` is a pure function of the variant
        and LIGHT always moves first — so storing it would be a second
        answer to a question the variant already settles, and one a rules
        fix would silently leave stale.

        `expected_result` is filled only for a completed match. A game still
        in progress has no result to check against, and asserting one would
        make every mid-game replay fail.
        """
        record = await self._matches.by_id(match_id)
        if record is None:
            return None

        variant = board_variant_of(record.variant)
        return ReplayData(
            engine_version=record.engine_version,
            variant=variant,
            opening_position=Position(board=initial_board(variant), side_to_move=PlayerSide.LIGHT),
            records=tuple(await self._moves.for_replay(match_id)),
            expected_result=record.result,
        )


__all__ = ["PersistedMatchReplay"]
