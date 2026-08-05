"""The SQLAlchemy adapter for `application.ports.StatisticsRepository`.

Database-only, per repositories.md §2: this class decides *how* to fetch,
never *whether* something may be fetched. Privacy is `profiles`' question
and is answered before this is ever called.

Two responsibilities beyond running SQL, both assigned here by
repositories.md §3:

  **mapping** — between `PlayerStatisticsModel` rows and `PlayerStatistics`
  values, so nothing above this layer holds an ORM object and inherits its
  lazy-loading and session-lifetime behaviour.

  **honest absence** — a player with no row returns `None`. Turning that
  into a default record is the service's decision, not storage's.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.statistics.domain.projection import Projected, ProjectionState
from app.modules.statistics.domain.statistics import PlayerStatistics
from app.modules.statistics.infrastructure.models import (
    PlayerStatisticsModel,
    ProcessedMatchModel,
)

logger = logging.getLogger(__name__)


class SqlAlchemyStatisticsRepository:
    """Constructed per use case with the active session
    (repositories.md §5.1) — never holds one longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def to_domain(row: PlayerStatisticsModel) -> PlayerStatistics:
        """Row to value object, field by field.

        Explicit rather than `PlayerStatistics(**row.__dict__)` for the
        reason `users.application.mappers` gives, plus one specific to a
        projection: the value object **validates on construction**, so an
        inconsistent row raises here rather than reaching a response. That
        is the point at which a broken rebuild is discovered, and it should
        be loud.
        """
        return PlayerStatistics(
            games_played=row.games_played,
            wins=row.wins,
            losses=row.losses,
            draws=row.draws,
            current_rating=row.current_rating,
            highest_rating=row.highest_rating,
            current_streak=row.current_streak,
            best_win_streak=row.best_win_streak,
        )

    # --- the writing half — A64-020.5F §4 --------------------------------
    #
    # Narrow on purpose. A projection needs exactly three operations: claim
    # a match for a player, read the row under a lock, and write it back.
    # Anything wider — "set games_played", "reset a player" — would be a
    # way to produce a row no sequence of matches could produce.

    async def claim(self, match_id: UUID, player_id: UUID, *, at: datetime) -> bool:
        """Records that this player has been credited with this match.

        `True` if this call made the record; `False` if it already existed.
        **The exactly-once mechanism** — `ON CONFLICT DO NOTHING` against
        `pk_processed_match`, so the decision is the database's and not a
        read this caller could lose a race on.

        Called **before** the counters move and in the same transaction, so
        a rollback takes both. Ordering it this way rather than marking
        afterwards means a crash between the two cannot leave a match
        counted and unmarked — which would double-count on retry, the one
        failure this whole mechanism exists to prevent. The opposite risk —
        marked and uncounted — cannot happen either, because there is no
        commit between them.
        """
        statement = (
            insert(ProcessedMatchModel)
            .values(match_id=match_id, player_id=player_id, processed_at=at)
            .on_conflict_do_nothing(constraint="pk_processed_match")
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return result.rowcount == 1

    async def state_for_update(self, player_id: UUID) -> ProjectionState:
        """This player's counters and watermark, with the row locked.

        `FOR UPDATE` and not `SKIP LOCKED`: two matches finishing for one
        player must both count, so the second waits and then reads what the
        first wrote. Skipping would silently drop a game.

        Created rather than refused for a player with no row, because the
        absence of a row is a legitimate state for a projection (DM-03) and
        a player's first completed match is exactly when one should appear.
        The insert races another consumer doing the same, so it is
        `ON CONFLICT DO NOTHING` followed by a locking read — the row exists
        either way by the time this returns.
        """
        await self._session.execute(
            insert(PlayerStatisticsModel)
            .values(player_id=player_id)
            .on_conflict_do_nothing(index_elements=[PlayerStatisticsModel.player_id])
        )
        row = await self._session.scalar(
            select(PlayerStatisticsModel)
            .where(PlayerStatisticsModel.player_id == player_id)
            .with_for_update()
        )
        if row is None:  # pragma: no cover — the insert above guarantees one
            raise RuntimeError(f"no statistics row for {player_id} after an upsert")

        # A **value**, not the row. The application layer folds counters and
        # must not hold an ORM object — `statistics layers point inward`
        # enforces it, and the practical reason is that a caller holding a
        # live row could write a field no projection rule produced.
        return ProjectionState(
            statistics=self.to_domain(row),
            counted_at=row.counted_at,
            counted_match_id=row.counted_match_id,
        )

    async def write(self, player_id: UUID, projected: Projected) -> None:
        """Stores a folded record.

        A statement rather than a mutation of the row `state_for_update`
        loaded, because that row is no longer in the caller's hands — and
        it needs no compare-and-set: the `FOR UPDATE` lock taken there is
        held for this transaction, so nothing can have moved in between.

        Deliberately does **not** touch `current_rating` or
        `highest_rating`. They are in this record because a profile renders
        them beside the counts, but they are `rating`'s facts — see
        `statistics.domain.projection`.
        """
        await self._session.execute(
            update(PlayerStatisticsModel)
            .where(PlayerStatisticsModel.player_id == player_id)
            .values(
                games_played=projected.statistics.games_played,
                wins=projected.statistics.wins,
                losses=projected.statistics.losses,
                draws=projected.statistics.draws,
                current_streak=projected.statistics.current_streak,
                best_win_streak=projected.statistics.best_win_streak,
                counted_at=projected.counted_at,
                counted_match_id=projected.counted_match_id,
            )
        )

    async def get_for_player(self, player_id: UUID) -> PlayerStatistics | None:
        # A primary-key lookup: `player_id` *is* the key, so this is an
        # index probe rather than a scan, which is what makes it acceptable
        # on the platform's highest-volume read (database.md §1436 routes
        # profile reads at a replica for the same reason).
        row = await self._session.scalar(
            select(PlayerStatisticsModel).where(PlayerStatisticsModel.player_id == player_id)
        )
        return self.to_domain(row) if row is not None else None

    async def get_for_players(self, player_ids: Sequence[UUID]) -> Mapping[UUID, PlayerStatistics]:
        """One statement for a whole page — A64-013.1.

        `IN (...)` over the primary key, so PostgreSQL probes the index once
        per id rather than scanning: the same access pattern as
        `get_for_player` above, batched, and still index-only work.

        `.in_()` rather than `= ANY(:array)`: SQLAlchemy renders it as an
        expanding bind parameter, which keeps the ids as separate parameters
        the planner can see, and stays correct on any driver rather than
        depending on asyncpg's array handling.

        Players with no row are simply absent from the mapping — the
        repository reports what storage holds, and `StatisticsService`
        decides what absence means (repositories.md §3's "honest absence",
        applied to a set).
        """
        if not player_ids:
            return {}

        rows = await self._session.scalars(
            select(PlayerStatisticsModel).where(PlayerStatisticsModel.player_id.in_(player_ids))
        )
        return {row.player_id: self.to_domain(row) for row in rows}
