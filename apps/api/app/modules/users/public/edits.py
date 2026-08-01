"""The published shapes of a partial update — `ProfileEdits` (A64-012.3),
`PrivacyEdits` (A64-012.4) and `PreferenceEdits` (A64-012.5).

All live in `public/` because BR-2 requires a published port to be defined
in terms of published types: `ProfileEditor.update_own_profile`,
`PrivacySettingsEditor.update_privacy_settings` and
`PreferencesEditor.update_preferences` accept them, so a consumer must be
able to construct one without importing anything from `users.application`.

The reasoning below is written about `ProfileEdits` and applies to all of
them; where one differs, its own docstring says so.

## Why every field defaults to `UNSET` rather than `None`

This is a PATCH, and a PATCH has three states per field, not two:

    absent            leave it alone
    present and null  clear it
    present and set   replace it

A shape using `None` for "absent" collapses the first two, so clearing a
biography and not mentioning it become the same request — and the one that
loses is always the clear, silently. `app.core.sentinels.UNSET` is the
third value that keeps them apart, and the HTTP layer maps Pydantic's
`model_fields_set` onto it.

## Why some fields carry `None` in their union and others do not

A field is `X | None | UnsetType` only when it genuinely has an empty
state. `ProfileEdits`' three are — removing a biography is a thing a
player legitimately does — so all three carry `None`.

Nothing on `PrivacyEdits`, `GameplayEdits` or `LocaleEdits` does. There is
no "no language", no "no board theme" and no "no answer" for a privacy
flag, so those are `X | UnsetType` and the type refuses a clear the entity
could not represent anyway. The request schemas reject an explicit `null`
for the same reason, rather than inventing a meaning for it.

## Raw strings, not value objects

A consumer hands over what the client typed; `UserService.update_profile`
constructs `DisplayName`, `Bio` and `CountryCode` from it. That direction
matters: if the consumer built the value objects, the validation would
happen in whichever module happened to call this, and a second consumer
would be free to skip it. Validating inside the service means every path
— HTTP today, an admin tool or a CLI tomorrow — is validated identically.
"""

from dataclasses import dataclass

from app.core.enums import Locale
from app.core.sentinels import UNSET, UnsetType
from app.modules.users.domain.preferences import AnimationSpeed, BoardTheme, PieceSet


@dataclass(frozen=True, slots=True)
class ProfileEdits:
    """The three fields a profile edit may change, and nothing else.

    A64-012.3 defined five. A64-012.5 removed `preferred_language` and
    `timezone`, which are preferences and now live on `PreferenceEdits`
    behind a different port — "avoid duplicated writable fields", and the
    duplication was real: both were reachable from a profile edit and were
    about to become reachable from a preferences update.

    Frozen, and deliberately closed: there is no `username`, no `email`,
    no `is_active`, no `avatar_object_key` and no `**extra`. **That is the
    mass-assignment defence**, and it is a property of the type rather than
    of a filter somebody has to remember to apply — a caller cannot set a
    field that does not exist, whatever arrives on the wire.

    Adding a sixth editable field is therefore a deliberate act with a
    visible diff, which is exactly the friction that surface should have.
    """

    display_name: str | None | UnsetType = UNSET
    bio: str | None | UnsetType = UNSET
    country: str | None | UnsetType = UNSET


@dataclass(frozen=True, slots=True)
class PrivacyEdits:
    """The five privacy flags A64-012.4 makes settable, and nothing else.

    Closed for the same reason `ProfileEdits` is, and the closure matters
    more here rather than less: this is the type a request body is mapped
    into on the endpoint that decides what strangers may see. A `**extra`
    would let a field nobody designed reach a service that writes to the
    account row.

    **No `None` in any of these unions**, unlike `ProfileEdits`. A boolean
    flag has two states and an account always has an answer, so there is no
    "clear it" to express — `UNSET` for absent, `True` or `False` for a
    value, and an explicit `null` on the wire is a client error the schema
    rejects rather than a third meaning invented here.

    Separate from `ProfileEdits` rather than five more fields on it. The two
    are edited on different screens, are read by different code, and carry
    different risk: getting a display name wrong is a cosmetic mistake,
    getting `show_last_seen` wrong publishes a person's schedule. They also
    go through different ports, which is what stops a component that may
    edit a biography from thereby being able to make an account's activity
    public.
    """

    show_country: bool | UnsetType = UNSET
    show_last_seen: bool | UnsetType = UNSET
    show_statistics: bool | UnsetType = UNSET
    show_online_status: bool | UnsetType = UNSET
    show_activity: bool | UnsetType = UNSET


@dataclass(frozen=True, slots=True)
class GameplayEdits:
    """The five gameplay settings A64-012.5 makes changeable.

    Closed, like every other edit shape here: five attributes and no
    `**extra`, so a caller cannot set a setting that does not exist
    whatever arrives on the wire.

    **No `None` in any union.** None of these has a cleared state — a
    player always has a board theme — so `UNSET` for absent and a real
    value otherwise is the whole vocabulary. An explicit `null` on the
    wire is a client error the request schema rejects.
    """

    board_theme: BoardTheme | UnsetType = UNSET
    piece_set: PieceSet | UnsetType = UNSET
    confirm_move: bool | UnsetType = UNSET
    show_coordinates: bool | UnsetType = UNSET
    animation_speed: AnimationSpeed | UnsetType = UNSET


@dataclass(frozen=True, slots=True)
class LocaleEdits:
    """The two locale settings A64-012.5 makes changeable.

    `timezone` is a raw string rather than a `Timezone`, for the reason
    `ProfileEdits` gives about raw strings generally: a consumer hands over
    what the client typed and `UserService.update_preferences` constructs
    the value object, so the IANA check happens inside the service and
    every path is validated identically. A consumer that built the value
    object would be doing validation this module is responsible for, and a
    second consumer would be free to skip it.
    """

    preferred_language: Locale | UnsetType = UNSET
    timezone: str | UnsetType = UNSET


@dataclass(frozen=True, slots=True)
class PreferenceEdits:
    """A partial preferences update, grouped — A64-012.5.

    **Three-state at two levels**, which is what a grouped PATCH needs and
    what a flat shape cannot express:

        gameplay is UNSET               leave the whole group alone
        gameplay set, board_theme UNSET leave that one setting alone
        gameplay set, board_theme given replace it

    Without the outer level, "the client did not mention gameplay" and "the
    client sent an empty gameplay object" would be the same request — and
    the natural implementation of the second is a group reset.

    Adding `ui` or `notifications` is one more attribute here and one more
    edit shape above, with nothing existing to change. That is the payoff
    for the nesting; a flat shape would need every new field prefixed by
    hand and would grow a naming convention nobody enforces.
    """

    gameplay: GameplayEdits | UnsetType = UNSET
    locale: LocaleEdits | UnsetType = UNSET
