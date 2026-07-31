"""`auth`'s storage adapters."""

from app.modules.auth.infrastructure.repositories.session_repository import (
    SqlAlchemySessionRepository,
)

__all__ = ["SqlAlchemySessionRepository"]
