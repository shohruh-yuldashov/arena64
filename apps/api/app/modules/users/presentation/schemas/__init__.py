"""Wire schemas for the `users` module."""

from app.modules.users.presentation.schemas.user import (
    UserCreate,
    UserList,
    UserRead,
    UserSummary,
    UserUpdate,
)

__all__ = ["UserCreate", "UserList", "UserRead", "UserSummary", "UserUpdate"]
