"""Wire schemas for the `users` module.

The request shapes are defined in `user.py`; the read shapes come from
`users/public/dtos.py`, because `auth` consumes them across a module
boundary too and a published DTO cannot live behind a presentation layer
(BR-2). Re-exported together here so every caller keeps one import path
regardless of which side a given shape is defined on.

`PublicUserResponse` (A64-012.6) is the exception in the other direction:
it is a *wire* shape and lives here, because it exists specifically so that
the published DTOs are no longer rendered raw onto an anonymous response.
See `public_user.py`.
"""

from app.modules.users.presentation.schemas.public_user import PublicUserResponse
from app.modules.users.presentation.schemas.user import UserCreate, UserList
from app.modules.users.public.dtos import UserRead, UserSummary

__all__ = ["PublicUserResponse", "UserCreate", "UserList", "UserRead", "UserSummary"]
