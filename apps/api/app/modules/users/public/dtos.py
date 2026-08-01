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


class PublicUserProfile(BaseResponseDTO):
    """What a stranger may see — A64-012.1's `GET /profiles/{username}`.

    ## Why this is a third read shape rather than a field on `UserRead`

    `UserRead` carries `email`. It is the account holder's *own* view, and
    its docstring has flagged since A64-010 that returning it for an
    arbitrary user id is a gap. A public profile endpoint is precisely the
    caller that would turn that gap into a disclosure, and "remember not to
    render the email field" is not a control — it is a convention that
    holds until the first careless `model_dump()`.

    So `profiles` never receives an email. Not "receives one and drops it":
    the port that serves it returns *this* type, which has no such field,
    so the leak is unreachable rather than merely avoided. That is the same
    reasoning `users` already applies five times over in `public/ports.py`,
    where creating an account, reading a password hash, reading a profile,
    confirming an address and replacing a credential are five separately
    grantable capabilities rather than one wide service.

    `UserSummary` was considered and is too thin — it has no join date, no
    country, no bio, and widening it would degrade the listing shape whose
    whole value is what it omits.

    ## What is deliberately absent

    No `email`, no `password_hash`, no `is_verified`, no `timezone`, no
    `locked_until`, no `updated_at`.

    The last four are the interesting omissions, because none is a
    credential and each is a small disclosure that adds up. `is_verified`
    and `locked_until` are account *state*: publishing them tells an
    attacker which accounts are half-registered or currently being
    attacked. `timezone` narrows a player's physical location. `updated_at`
    reveals when somebody last touched their account, which is activity
    metadata a stranger has no claim to and which A64-012.1's constraints
    exclude under "online status".

    `is_active` is absent for a different reason: a deactivated account has
    no public profile at all, so the flag would be constant-true wherever
    it could be read. `ProfileService` enforces that rather than exposing
    it — see its `find_by_username`.
    """

    id: UUID
    """The player identifier — DM-06's `player_id`, the only reference that
    crosses a context boundary.

    A64-012.1's "never expose internal ids" means session ids, token ids
    and row identifiers of `auth`'s tables. This one is public by design:
    it is what a future match record, rating row or leaderboard entry
    refers to a player by, and a client that could not obtain it could not
    fetch anything about them.
    """

    username: str
    display_name: str | None
    avatar_url: str | None
    country: str | None
    """ISO 3166-1 alpha-2, upper-cased. `None` until profile editing
    exists — no endpoint writes it yet."""

    preferred_language: Locale
    bio: str | None
    """Plain text, at most 500 characters, already trimmed and free of
    control and bidirectional characters (`validate_bio`). Not Markdown —
    a renderer must not interpret it."""

    created_at: datetime
    """The join date. Named `created_at` here to match every other DTO on
    the platform and rendered as `joined_at` on the wire, where it is the
    word a player understands — see `profiles`' response schema."""
