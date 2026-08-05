"""`MatchHistoryStore` over `game.match` — SPEC-REPLAY §1, §7.

One keyset query per page, and one row read for a single entry. No lock, no
write, and no move log: a history page is stored facts about matches nobody
will touch again.

## Keyset, not offset

Ordered `created_at DESC, id DESC`, and the cursor carries both. `OFFSET`
re-scans and shifts when a match finishes between two page reads, so a
player paging back through their record could see a game twice or miss one.
`id` is the tiebreaker of last resort and is unique, so the order is total —
which is what makes the cursor unable to skip.

## Finished means settled

`settled_at IS NOT NULL` and a terminal status. A pending or active match is
not history: it has no result to show, and it will change.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.infrastructure.models import MatchRecordModel
from app.modules.game.public.history import (
    CompletedMatchRecord,
    HistoryCursor,
    MatchHistoryEntry,
    MatchHistoryPage,
)
from app.modules.game.public.matches import MatchTimeControl

#: The most matches one page may return — §10.5's "every list endpoint
#: paginates".
MAX_PAGE_SIZE: Final = 100
DEFAULT_PAGE_SIZE: Final = 20

#: What "finished" means here. `COMPLETED` only: a cancelled or expired
#: match never became a game, and A64-015.5's retention deletes those — a
#: history that listed them would show rows that vanish.
_FINISHED: Final = MatchRecordStatus.COMPLETED


class SqlAlchemyMatchHistoryRepository:
    """`MatchHistoryStore` over one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def finished_for(
        self, player_id: UUID, *, after: HistoryCursor | None, limit: int
    ) -> MatchHistoryPage:
        """One page of this player's finished matches, newest first."""
        wanted = max(1, min(limit, MAX_PAGE_SIZE))

        statement = (
            select(MatchRecordModel)
            .where(
                MatchRecordModel.status == _FINISHED,
                or_(
                    MatchRecordModel.light_player_id == player_id,
                    MatchRecordModel.dark_player_id == player_id,
                ),
            )
            .order_by(MatchRecordModel.created_at.desc(), MatchRecordModel.id.desc())
            .limit(wanted + 1)
        )

        if after is not None:
            statement = statement.where(
                tuple_(MatchRecordModel.created_at, MatchRecordModel.id)
                < tuple_(literal(after.created_at), literal(after.match_id))
            )

        rows = list(await self._session.scalars(statement))
        page, has_more = rows[:wanted], len(rows) > wanted
        entries = [_to_entry(row) for row in page]

        return MatchHistoryPage(
            entries=entries,
            next_cursor=(
                HistoryCursor(created_at=page[-1].created_at, match_id=page[-1].id)
                if has_more and page
                else None
            ),
        )

    async def finished_entry(self, match_id: UUID) -> MatchHistoryEntry | None:
        row = await self._session.get(MatchRecordModel, match_id)
        if row is None or row.status != _FINISHED:
            return None
        return _to_entry(row)

    async def scan_completed(
        self, *, after: tuple[datetime, UUID] | None, limit: int
    ) -> Sequence[CompletedMatchRecord]:
        """One page of every finished match, oldest first — A64-020.5F §11.

        **Keyset on `(ended_at, id)`**, which is the same total order the
        projection compares watermarks with — so a backfill folds matches in
        the order they happened and produces the streaks the live consumer
        would have.

        `ended_at` is non-null for a completed match by
        `ck_match__ended_at_iff_outcome`, so the tuple comparison is total
        and no row sorts unpredictably.
        """
        statement = (
            select(MatchRecordModel)
            .where(MatchRecordModel.status == _FINISHED)
            .order_by(MatchRecordModel.ended_at.asc(), MatchRecordModel.id.asc())
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(
                tuple_(MatchRecordModel.ended_at, MatchRecordModel.id)
                > tuple_(literal(after[0]), literal(after[1]))
            )

        rows = await self._session.scalars(statement)
        return [_to_completed(row) for row in rows]


def _to_completed(row: MatchRecordModel) -> CompletedMatchRecord:
    """A finished row as the projection's narrow input.

    `ended_at` and `outcome` are non-null on a completed row by CHECK
    constraint, so the assertions below are documentation of an invariant
    the database already holds rather than a guard against data.
    """
    if row.ended_at is None or row.outcome is None or row.termination_reason is None:
        raise ValueError(f"match {row.id} is completed without a result")
    return CompletedMatchRecord(
        match_id=row.id,
        light_player_id=row.light_player_id,
        dark_player_id=row.dark_player_id,
        outcome=row.outcome,
        winner=row.winner,
        rated=row.rated,
        termination_reason=row.termination_reason,
        completed_at=row.ended_at,
    )


def _to_entry(row: MatchRecordModel) -> MatchHistoryEntry:
    return MatchHistoryEntry(
        match_id=row.id,
        variant=row.variant,
        rated=row.rated,
        engine_version=row.engine_version,
        light_player_id=row.light_player_id,
        dark_player_id=row.dark_player_id,
        outcome=row.outcome,
        termination_reason=row.termination_reason,
        winner=row.winner,
        time_control=(
            MatchTimeControl(
                initial_ms=row.time_control_initial_ms,
                increment_ms=row.time_control_increment_ms or 0,
            )
            if row.time_control_initial_ms is not None
            else None
        ),
        # Read from the row rather than left as `None` — A64-020.5F. The
        # schema has declared this field since A64-014.9 and the mapper
        # hardcoded a null under it, so every history row served claimed the
        # match had no speed class.
        speed_class=row.rating_speed_class,
        ply_number=row.ply_number,
        ended_at=row.ended_at,
        created_at=row.created_at,
    )


__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "SqlAlchemyMatchHistoryRepository"]
