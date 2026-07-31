"""Wire schemas — Pydantic v2, built on the platform DTO bases from
`app.core.dto` (A64-008) rather than on `BaseModel` directly, so request
strictness and response `from_attributes` behaviour are declared once for
the whole platform instead of per module.

**Validation is reused, not re-stated.** Every constraint here calls into
`domain/validators.py` through an `AfterValidator`. That is what makes the
task's "validation should be reusable" true in the direction that matters:
a rule change lands in one function and applies to the HTTP boundary, the
service, and the domain constructor at once. Re-expressing the length
bound as a Pydantic `Field(min_length=3)` would be a second copy that
drifts the first time the rule changes — as it did in A64-011.1, when the
maximum went from 32 to 20.

The **read** shapes (`UserRead`, `UserSummary`) now live in
`users/public/dtos.py` and are imported here. They moved when `auth`
became a second consumer of them across a module boundary — see that
module for why a published DTO cannot live in a presentation layer. The
names are unchanged, so nothing that used them had to change.

`password_hash` appears on `UserCreate` and on **no read schema**, which is
the one thing in this file that must never regress.
"""

from typing import Annotated

from pydantic import AfterValidator, Field, model_validator

from app.core.dto import BaseRequestDTO
from app.core.enums import Locale
from app.core.pagination import CursorPage
from app.modules.users.domain.exceptions import InvalidLanguage, InvalidTimezone
from app.modules.users.domain.validators import (
    AVATAR_URL_MAX_LENGTH,
    DISPLAY_NAME_MAX_LENGTH,
    validate_email,
    validate_timezone,
    validate_username,
)
from app.modules.users.public.dtos import UserSummary

# The reusable annotated types. Each wraps the single domain validator, so
# a violation raises this module's typed domain error (`InvalidUsername`,
# ...) which the existing handler table already maps to a 422 — no
# per-schema error handling anywhere.
UsernameField = Annotated[str, AfterValidator(validate_username)]
EmailField = Annotated[str, AfterValidator(validate_email)]
TimezoneField = Annotated[str, AfterValidator(validate_timezone)]
DisplayNameField = Annotated[str, Field(min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH)]
AvatarUrlField = Annotated[str, Field(min_length=1, max_length=AVATAR_URL_MAX_LENGTH)]


class UserCreate(BaseRequestDTO):
    """Inputs for creating a user.

    Not bound to any route in A64-010 — there is no registration endpoint
    by the task's constraint. It exists because `auth` (A64-011) is the
    thing that will own that route, and this is the shape it will post
    into `UserService.create_user`.

    `password_hash`, not `password`: this module never hashes (the task's
    explicit constraint). Whoever calls this has already applied the
    platform's hashing parameters. When A64-011 adds a real registration
    endpoint it should expose a `password` field on *its own* schema and
    hash before reaching this one — a public endpoint accepting a
    pre-computed hash would let a caller choose their own weak one.
    """

    username: UsernameField
    email: EmailField
    password_hash: str = Field(min_length=1, max_length=255)
    preferred_language: Locale = Locale.EN
    timezone: TimezoneField = "UTC"
    display_name: DisplayNameField | None = None
    avatar_url: AvatarUrlField | None = None


class UserUpdate(BaseRequestDTO):
    """Partial profile update — every field optional.

    All four are `| None` *and* default to unset, which are different
    states: omitting `display_name` leaves it alone, sending
    `"display_name": null` clears it. The route reads
    `model_fields_set` to tell them apart and maps to `UNSET` accordingly
    (`app.core.sentinels`).

    `username`, `email`, `is_active` and `is_verified` are absent by
    design — see `application/commands.py::UpdateUserProfile` for why each
    is its own use case rather than a PATCH field.
    """

    display_name: DisplayNameField | None = None
    avatar_url: AvatarUrlField | None = None
    preferred_language: Locale | None = None
    timezone: TimezoneField | None = None

    @model_validator(mode="after")
    def _reject_explicit_null_on_non_clearable(self) -> "UserUpdate":
        """`display_name` and `avatar_url` are nullable on the entity, so
        an explicit `null` legitimately clears them. `preferred_language`
        and `timezone` are `NOT NULL` with defaults — there is no state
        for `null` to mean, so accepting it would force a silent choice
        between "leave alone" and "reset to default", and a client could
        never tell which it got. Rejecting is the only unambiguous answer
        (CLAUDE.md §9 rule 1: reject invalid input at the edge).

        Raises this module's own domain errors rather than a `ValueError`
        so the failure carries the same typed code and envelope as every
        other error on the platform — Pydantic only intercepts
        `ValueError`/`AssertionError`, so an `Arena64Error` propagates
        cleanly to the platform handler.
        """
        if "preferred_language" in self.model_fields_set and self.preferred_language is None:
            raise InvalidLanguage(
                "preferred_language cannot be null; omit the field to leave it unchanged."
            )
        if "timezone" in self.model_fields_set and self.timezone is None:
            raise InvalidTimezone("timezone cannot be null; omit the field to leave it unchanged.")
        return self


# The paginated listing shape. A plain alias over the platform's
# `CursorPage` (A64-008) rather than a new class: the envelope is already
# the platform's contract, and redeclaring `items`/`page` here would be
# the duplication CLAUDE.md §2.1 warns about. Keyset-paginated per RP-03.
UserList = CursorPage[UserSummary]
