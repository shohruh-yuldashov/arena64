"""What the admin Users API returns — A64-024.3 §5, §6.

**Every field here is a deliberate exposure.** The password hash, refresh
and access tokens, OTP material, session records and provider responses are
absent from these types and from the port beneath them, so there is no
serialisation path that could carry one.

`email` is present on purpose: an operator's starting point is a support
request, and a support request carries an address. Omitting it would push
operators to `psql`, which is a worse place for this data to be read.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AdminUserSummary(BaseModel):
    """One account in the list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    username: str
    display_name: str | None = None
    email: str
    is_active: bool
    is_verified: bool
    created_at: datetime
    is_admin: bool = Field(
        description="Whether this account currently holds the admin role. "
        "Resolved per page in one batch read, never per row."
    )


class AdminUserPageResponse(BaseModel):
    """One page, and the cursor that continues it.

    **No total count** — an operator needs "are there more", and a count on
    this table is a sequential scan on every page of every search.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[AdminUserSummary]
    next_cursor: str | None = None


class AdminUserDetail(BaseModel):
    """One account in full — §6.

    Composed from published ports only: `users` for identity and account
    state, `admin` for the role. Nothing here reads another module's
    storage.

    **No rating summary**, deliberately — see the router on why "if cheap"
    is not satisfied by a reader batched on `(player, key)` pairs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    username: str
    display_name: str | None = None
    email: str
    is_active: bool
    is_verified: bool
    created_at: datetime
    is_admin: bool
    admin_role_granted_at: datetime | None = Field(
        default=None,
        description="When the live admin grant was made. Absent when the account holds none.",
    )


__all__ = [
    "AdminUserDetail",
    "AdminUserPageResponse",
    "AdminUserSummary",
]
