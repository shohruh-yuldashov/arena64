"""`SqlAlchemyMoveLogRepository` — the append-only move log. A64-016.4 §1.

Two operations and no third. There is no `update`, no `delete` and no
`replace`: MT-5 makes the log append-only, and a repository that could
amend an entry would make "append-only" a convention rather than a
property.

## Why `append` does not check for a duplicate

It inserts, and lets `uq_move__ply` refuse. §2 is explicit — "do not rely on
in-memory deduplication" — and a check-then-insert would be exactly that:
two concurrent submissions for the same ply both pass the check, both
insert, and one of them succeeds only because the database refused the
other. Doing it that way makes the index the mechanism and the check
decoration.

The caller catches the integrity error and turns it into `StaleMatchState`.

## Why the read is ordered and unbounded

`for_replay` returns the whole log. Every other list read on this platform
paginates (CLAUDE.md §10.5) and this one deliberately does not: a replay
that saw a page would reconstruct a *different game*, silently, and there
is no correct page size for "all of it". The bound is the game — a draughts
match is tens of plies, and the move limit rules bound it above.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.engine import BoardCoordinate, Move, PieceRank
from app.modules.game.application.ports import LoggedMove
from app.modules.game.domain.move_log import MoveRecord
from app.modules.game.infrastructure.models import MoveLogModel

logger = logging.getLogger(__name__)


class SqlAlchemyMoveLogRepository:
    """`MoveLogRepository` over `game.move`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, match_id: UUID, entry: LoggedMove) -> None:
        """Appends one move.

        **Flushes, never commits.** The caller's unit of work spans this
        row, the match's new ply and the outbox event — one transaction,
        because §3 requires "match advanced but move record missing" to be
        impossible.

        The flush is what makes `uq_move__ply` fire *here* rather than at
        commit, so the caller can catch it and answer the client rather
        than losing the whole transaction to a deferred constraint.
        """
        self._session.add(
            MoveLogModel(
                match_id=match_id,
                ply_number=entry.record.ply_number,
                seat=entry.seat,
                path=[str(square) for square in entry.record.move.path],
                captured=[str(square) for square in entry.record.move.captured],
                promoted_to=(
                    entry.record.move.promotes_to.value
                    if entry.record.move.promotes_to is not None
                    else None
                ),
                position_hash=entry.record.resulting_position_hash,
                engine_version=entry.engine_version.number,
                think_time_ms=entry.record.think_time_ms,
                remaining_clock_ms=entry.record.remaining_clock_ms,
                received_at=entry.received_at,
                created_at=entry.created_at,
            )
        )
        await self._session.flush()

    async def for_replay(self, match_id: UUID) -> Sequence[MoveRecord]:
        """Every move of one match, in order — the input to a replay.

        Ordered by `ply_number` and served by `ix_move__replay`, so the
        ordering comes from the index rather than from a sort. Contiguity
        is *not* checked here: `ReplayEngine._require_contiguous` already
        does it and raises `MalformedMoveLog`, which is a better error than
        anything this layer could invent.
        """
        rows = await self._session.scalars(
            select(MoveLogModel)
            .where(MoveLogModel.match_id == match_id)
            .order_by(MoveLogModel.ply_number)
        )
        return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_domain(row: MoveLogModel) -> MoveRecord:
        """One row as the record `ReplayEngine` consumes.

        Reconstructs a real `Move`, which re-runs its own validation — a
        path of one square or a repeated capture fails here rather than
        inside a replay, where it would surface as a rules error and send
        somebody looking at the engine.
        """
        return MoveRecord(
            ply_number=row.ply_number,
            move=Move(
                path=tuple(BoardCoordinate.parse(square) for square in row.path),
                captured=tuple(BoardCoordinate.parse(square) for square in row.captured),
                promotes_to=PieceRank(row.promoted_to) if row.promoted_to else None,
            ),
            resulting_position_hash=row.position_hash,
            think_time_ms=row.think_time_ms,
            remaining_clock_ms=row.remaining_clock_ms,
        )


__all__ = ["SqlAlchemyMoveLogRepository"]
