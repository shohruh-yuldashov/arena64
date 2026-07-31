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
    avatar_url: str | None = None


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
    values a client sets.
    """

    display_name: str | None | UnsetType = UNSET
    avatar_url: str | None | UnsetType = UNSET
    preferred_language: Locale | UnsetType = UNSET
    timezone: str | UnsetType = UNSET
