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
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.modules.engine import PlayerSide, Position, initial_board
from app.modules.game.application.ports import MatchRecordRepository, MoveLogRepository
from app.modules.game.domain.match_record import MatchRecord
from app.modules.game.domain.move_log import MoveRecord
from app.modules.game.domain.replay import ReplayData
from app.modules.game.domain.variants import board_variant_of

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReplaySource:
    """Everything one replay needs, from one read of the match row —
    A64-020.5E.

    A pair rather than a mutable attribute on the builder: the caller needs
    the rules input *and* the platform metadata, and reading the row twice
    to get both would be a second query for facts already in hand. Frozen,
    so nothing downstream can mistake it for a place to accumulate state.
    """

    data: ReplayData
    record: MatchRecord


class PersistedMatchReplay:
    """Builds `ReplayData` from a stored match and its move log."""

    def __init__(self, *, matches: MatchRecordRepository, moves: MoveLogRepository) -> None:
        self._matches = matches
        self._moves = moves

    async def source_for(self, match_id: UUID) -> ReplaySource | None:
        """The replay input and the match row together, from one read.

        `replay_data` below is kept for the callers that only need the
        rules input — `GameMatchSnapshot` is one — so this does not widen
        what they hold.
        """
        record = await self._matches.by_id(match_id)
        if record is None:
            return None
        return ReplaySource(
            data=self._data_for(record, await self._moves.for_replay(match_id)), record=record
        )

    @staticmethod
    def _data_for(record: MatchRecord, moves: Sequence[MoveRecord]) -> ReplayData:
        variant = board_variant_of(record.variant)
        return ReplayData(
            engine_version=record.engine_version,
            variant=variant,
            opening_position=Position(board=initial_board(variant), side_to_move=PlayerSide.LIGHT),
            records=tuple(moves),
            expected_result=record.result,
        )

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
        source = await self.source_for(match_id)
        return source.data if source is not None else None


__all__ = ["PersistedMatchReplay", "ReplaySource"]
