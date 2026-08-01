"""The published shapes of a partial update — `ProfileEdits` (A64-012.3)
and `PrivacyEdits` (A64-012.4).

Both live in `public/` because BR-2 requires a published port to be defined
in terms of published types: `ProfileEditor.update_own_profile` and
`PrivacySettingsEditor.update_privacy_settings` accept them, so a consumer
must be able to construct one without importing anything from
`users.application`.

The reasoning below is written about `ProfileEdits` and applies to both;
where `PrivacyEdits` differs — it has no nullable field, because a boolean
has no cleared state — its own docstring says so.

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

## Why the two non-nullable fields are typed differently

`preferred_language` and `timezone` are `UNSET`-or-value, with no `None`.
An account always has both — there is no "no language" state — so the type
refuses a clear that the entity could not represent anyway. The three
decorative fields are `str | None | UnsetType`, because removing a bio is
a thing a player legitimately does.

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


@dataclass(frozen=True, slots=True)
class ProfileEdits:
    """The five fields A64-012.3 makes editable, and nothing else.

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
    preferred_language: Locale | UnsetType = UNSET
    timezone: str | UnsetType = UNSET


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
