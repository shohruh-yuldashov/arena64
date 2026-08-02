"""`game`'s repositories — one per aggregate root (repositories.md)."""

from app.modules.game.infrastructure.repositories.match_record_repository import (
    SqlAlchemyMatchRecordRepository,
)

__all__ = ["SqlAlchemyMatchRecordRepository"]
