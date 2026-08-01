"""`matchmaking`'s repository adapters — one per aggregate root."""

from app.modules.matchmaking.infrastructure.repositories.queue_repository import (
    SqlAlchemyQueueRepository,
)

__all__ = ["SqlAlchemyQueueRepository"]
