"""Storage adapters for `statistics`."""

from app.modules.statistics.infrastructure.repositories.statistics_repository import (
    SqlAlchemyStatisticsRepository,
)

__all__ = ["SqlAlchemyStatisticsRepository"]
