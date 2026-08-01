"""The `statistics` domain — framework-free (architecture.md §8)."""

from app.modules.statistics.domain.statistics import (
    DEFAULT_RATING,
    NO_MATCHES_PLAYED,
    WIN_RATE_PRECISION,
    PlayerStatistics,
)

__all__ = [
    "DEFAULT_RATING",
    "NO_MATCHES_PLAYED",
    "WIN_RATE_PRECISION",
    "PlayerStatistics",
]
