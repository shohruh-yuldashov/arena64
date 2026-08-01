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
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.statistics.domain.statistics import PlayerStatistics
from app.modules.statistics.infrastructure.models import PlayerStatisticsModel

logger = logging.getLogger(__name__)


class SqlAlchemyStatisticsRepository:
    """Constructed per use case with the active session
    (repositories.md §5.1) — never holds one longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: PlayerStatisticsModel) -> PlayerStatistics:
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

    async def get_for_player(self, player_id: UUID) -> PlayerStatistics | None:
        # A primary-key lookup: `player_id` *is* the key, so this is an
        # index probe rather than a scan, which is what makes it acceptable
        # on the platform's highest-volume read (database.md §1436 routes
        # profile reads at a replica for the same reason).
        row = await self._session.scalar(
            select(PlayerStatisticsModel).where(PlayerStatisticsModel.player_id == player_id)
        )
        return self._to_domain(row) if row is not None else None
