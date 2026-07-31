"""Repository adapters."""

from app.modules.users.infrastructure.repositories.user_repository import (
    SqlAlchemyUserRepository,
)

__all__ = ["SqlAlchemyUserRepository"]
