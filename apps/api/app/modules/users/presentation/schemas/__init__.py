"""Wire schemas for the `users` module.

The request shapes are defined in `user.py`; the read shapes come from
`users/public/dtos.py`, because `auth` consumes them across a module
boundary too and a published DTO cannot live behind a presentation layer
(BR-2). Re-exported together here so every caller keeps one import path
regardless of which side a given shape is defined on.
"""

from app.modules.users.presentation.schemas.user import UserCreate, UserList
from app.modules.users.public.dtos import UserRead, UserSummary

__all__ = ["UserCreate", "UserList", "UserRead", "UserSummary"]
