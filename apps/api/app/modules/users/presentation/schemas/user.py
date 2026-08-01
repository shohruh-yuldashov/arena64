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

from pydantic import AfterValidator, Field

from app.core.dto import BaseRequestDTO
from app.core.enums import Locale
from app.core.pagination import CursorPage
from app.modules.users.domain.validators import (
    validate_display_name,
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
#: A64-012.3 gave this field a real domain validator, so this annotation
#: wraps it like `UsernameField` and `EmailField` do rather than restating
#: a bare length bound. The bound moved with it (1-64 -> 3-50) and now
#: lives in exactly one place; trimming and the control-character rules
#: come along for free, which the previous `Field(min_length=1, ...)` had
#: no way to express.
DisplayNameField = Annotated[str, AfterValidator(validate_display_name)]


# --- removed in A64-012.3: `UserUpdate` --------------------------------------
#
# The request body of `PATCH /users/{user_id}`, which A64-012.3 retired —
# see `presentation/router.py` for why an unauthenticated edit-any-player
# endpoint could not coexist with "only the profile owner may edit".
#
# Its replacement is `profiles.presentation.schemas.ProfileUpdateRequest`,
# which carries two more editable fields (`bio`, `country`) and is reached
# only through an authenticated, self-scoped route.


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


# The paginated listing shape. A plain alias over the platform's
# `CursorPage` (A64-008) rather than a new class: the envelope is already
# the platform's contract, and redeclaring `items`/`page` here would be
# the duplication CLAUDE.md §2.1 warns about. Keyset-paginated per RP-03.
UserList = CursorPage[UserSummary]
