"""Command objects — the typed inputs `UserService` accepts.

services.md §3.3 prohibits a service from accepting a Pydantic *request*
model: it would couple the use case to one wire format, so a v2 API or a
non-HTTP caller (a future admin tool, a Celery task) would force a service
rewrite. These plain dataclasses are what the presentation layer maps its
schemas *into*.

`UpdateUserProfile` uses `UNSET` (`app.core.sentinels`) rather than `None`
defaults because a PATCH has three states per field and `None` can only
carry two — see that module's docstring for the full reasoning. Without it,
"don't touch my display name" and "clear my display name" are the same
request.
"""

from dataclasses import dataclass

from app.core.enums import Locale
from app.core.sentinels import UNSET, UnsetType


@dataclass(frozen=True, slots=True)
class CreateUser:
    """Inputs for a new user.

    `password_hash` is an **already-hashed** credential. This module never
    hashes, verifies, or compares it (the task's explicit constraint, and
    the right boundary regardless: hashing parameters are a security
    decision that belongs with `auth`, which owns rotation and rehashing).
    Callers before A64-011 must supply a hash they produced themselves.
    """

    username: str
    email: str
    password_hash: str
    preferred_language: str
    timezone: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateUserProfile:
    """Partial profile update. Every field defaults to `UNSET` — absent.

    Deliberately excludes `username` and `email`. Neither is a plain
    profile field:

      - Changing a username has to record the old one and enforce a reuse
        cooldown (domain-model.md UP-2/UP-3), or a rename becomes a free
        identity handoff — a use case of its own, not a PATCH field.
      - Changing an email has to un-verify the account and re-prove
        ownership, which is `auth`'s flow. Letting it through here would
        let anyone move a verified account onto an address they never
        proved they own.

    Also excludes `is_active` and `is_verified`: those are state
    transitions with their own service methods and their own meaning, not
    values a client sets. And `avatar_object_key`, which A64-012.2 made
    writable only by uploading an image that the platform itself validates
    and stores — a client-supplied key would point an avatar at any object
    in the bucket.

    Three of the five fields are nullable (`display_name`, `bio`,
    `country`) and two are not (`preferred_language`, `timezone`). That is
    the difference between a decoration a player may remove and a setting
    that must always have a value — the schema enforces it at the boundary
    so an explicit `null` on the latter two is a 422 rather than a silent
    no-op.

    **Raw strings, not value objects.** The command is the boundary
    between a caller and the service, and a caller that had to construct
    `Bio`/`CountryCode`/`DisplayName` would be doing the validation this
    service exists to guarantee. `update_profile` constructs them, so
    every path — HTTP, a future CLI, a test — is validated identically.
    """

    display_name: str | None | UnsetType = UNSET
    bio: str | None | UnsetType = UNSET
    country: str | None | UnsetType = UNSET
    preferred_language: Locale | UnsetType = UNSET
    timezone: str | UnsetType = UNSET


@dataclass(frozen=True, slots=True)
class UpdatePrivacySettings:
    """Partial privacy update — A64-012.4. Every flag defaults to `UNSET`.

    A separate command from `UpdateUserProfile` rather than five more
    fields on it, mirroring the split between `ProfileEdits` and
    `PrivacyEdits` on the published surface. The two are written by
    different use cases with different rate limits and different
    consequences, and a single command would make "which of these did the
    caller mean to touch" a question with ten answers instead of five.

    **No `None` in any union**, unlike `UpdateUserProfile`. A boolean flag
    has no cleared state, so the three-state problem collapses to two: set,
    or absent. That is also what lets `PrivacySettings.updated()` use
    `None` for "unchanged" without ambiguity — see `domain/privacy.py`.

    Deliberately carries no user id. It says *what* to change, not *whose*;
    the account comes from the identifier the caller has already
    authenticated, exactly as `UpdateUserProfile` does.
    """

    show_country: bool | UnsetType = UNSET
    show_last_seen: bool | UnsetType = UNSET
    show_statistics: bool | UnsetType = UNSET
    show_online_status: bool | UnsetType = UNSET
    show_activity: bool | UnsetType = UNSET
