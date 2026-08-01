"""Application services — one class per cohesive set of use cases
(services.md §3)."""

from app.modules.statistics.application.services.statistics_service import StatisticsService

__all__ = ["StatisticsService"]
