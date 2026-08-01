"""Storage for `statistics` — the ORM model and its adapter."""

from app.modules.statistics.infrastructure.models import (
    STATISTICS_SCHEMA,
    PlayerStatisticsModel,
)

__all__ = ["STATISTICS_SCHEMA", "PlayerStatisticsModel"]
