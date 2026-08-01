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
from app.modules.users.domain.preferences import AnimationSpeed, BoardTheme, PieceSet
from app.modules.users.domain.visibility import VisibilityLevel


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

    **Since A64-012.5 it also excludes `preferred_language` and
    `timezone`.** Both were here, and both are preferences by every
    definition domain-model.md §7.1 uses — leaving them writable from a
    profile edit *and* from a preferences update would be the duplicated
    writable field that task set out to remove. They live on
    `UpdatePreferences` now, and a profile edit cannot reach them.

    All three remaining fields are nullable, which is the shape that made
    `UNSET` necessary in the first place: each is a decoration a player may
    legitimately remove, so "leave it alone" and "clear it" have to stay
    distinguishable.

    **Raw strings, not value objects.** The command is the boundary
    between a caller and the service, and a caller that had to construct
    `Bio`/`CountryCode`/`DisplayName` would be doing the validation this
    service exists to guarantee. `update_profile` constructs them, so
    every path — HTTP, a future CLI, a test — is validated identically.
    """

    display_name: str | None | UnsetType = UNSET
    bio: str | None | UnsetType = UNSET
    country: str | None | UnsetType = UNSET


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
    last_seen: VisibilityLevel | UnsetType = UNSET
    show_statistics: bool | UnsetType = UNSET
    online_status: VisibilityLevel | UnsetType = UNSET
    activity: VisibilityLevel | UnsetType = UNSET


@dataclass(frozen=True, slots=True)
class UpdateGameplayPreferences:
    """The gameplay group of a partial preferences update — A64-012.5."""

    board_theme: BoardTheme | UnsetType = UNSET
    piece_set: PieceSet | UnsetType = UNSET
    confirm_move: bool | UnsetType = UNSET
    show_coordinates: bool | UnsetType = UNSET
    animation_speed: AnimationSpeed | UnsetType = UNSET


@dataclass(frozen=True, slots=True)
class UpdateLocalePreferences:
    """The locale group of a partial preferences update — A64-012.5.

    `timezone` is a raw string for the reason `UpdateUserProfile`'s fields
    are: the command is the boundary, and a caller that had to construct a
    `Timezone` would be performing the IANA check this service exists to
    guarantee. `preferred_language` is already a closed enum, so there is
    nothing left for a value object to validate.
    """

    preferred_language: Locale | UnsetType = UNSET
    timezone: str | UnsetType = UNSET


@dataclass(frozen=True, slots=True)
class UpdatePreferences:
    """Partial preferences update — A64-012.5.

    **Nested, not flat**, and the nesting is the design rather than
    decoration. A group is the unit the API patches, the unit the log
    records ("updated groups"), and the unit `notifications` and `ui` are
    added as. A flat command would make "was any gameplay setting touched"
    a question answered by a hand-maintained list of field names.

    It is also three-state at *two* levels, which a flat shape could not
    express: an absent group is untouched, and inside a present group an
    absent field is untouched. `{"gameplay": {"board_theme": "wood"}}`
    changes one setting and leaves a language alone; `{}` changes nothing
    at all.

    Deliberately excludes privacy, which domain-model.md §7.1 does list as
    a preference group. It has its own command, port and endpoint already —
    see `users.domain.preferences` for why a control over what *strangers*
    see does not belong on the same write as a board theme.
    """

    gameplay: UpdateGameplayPreferences | UnsetType = UNSET
    locale: UpdateLocalePreferences | UnsetType = UNSET
