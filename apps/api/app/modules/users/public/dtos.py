"""The read shapes `users` publishes.

These live in `public/` rather than in `presentation/schemas/` because
they are now consumed in two directions: this module's own router renders
them, and `auth` receives them across a module boundary (BR-2: a
`public/` port is defined in terms of `public/` DTOs only). Defining them
here and importing downward is what keeps `application/` — which returns
them from the published port — from having to import `presentation/`,
which would be an upward import and exactly the layering inversion
architecture.md §8 forbids.

A64-010 defined them in `presentation/schemas/user.py`; that module now
imports them from here, so the names every existing caller uses are
unchanged.

Neither carries `password_hash`, and neither ever will — see
`app/modules/auth/` for who owns credentials.
"""

from datetime import datetime
from uuid import UUID

from app.core.dto import BaseResponseDTO
from app.core.enums import Locale


class UserRead(BaseResponseDTO):
    """The full view of a user's own account.

    Includes `email`, which is the account holder's own address. There is
    no authorisation layer yet to distinguish "my profile" from "someone
    else's", so this shape is currently returned for *any* user id — a gap
    A64-011.2 must close by splitting a self view from a public one. It is
    recorded here as well as in the router because this is the type that
    would carry the leak.
    """

    id: UUID
    username: str
    email: str
    display_name: str | None
    avatar_url: str | None
    preferred_language: Locale
    timezone: str
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime | None


class UserSummary(BaseResponseDTO):
    """The minimal public view — what a list, a search result, or a future
    match card needs. Deliberately excludes email, verification state and
    timestamps: a listing is the highest-volume read on any user table, and
    the fields it omits are the ones that would make it a privacy question
    rather than a rendering one.
    """

    id: UUID
    username: str
    display_name: str | None
    avatar_url: str | None
