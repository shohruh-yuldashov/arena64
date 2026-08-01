"""Wire schemas for the preferences endpoints — A64-012.5.

Two request groups, two response groups, and the two envelopes that carry
them.

## Why the wire shape is grouped

`{"gameplay": {...}, "locale": {...}}` rather than eight sibling keys. The
group is the unit the API patches, the unit the log records ("updated
groups"), and the unit `ui` and `notifications` are added as — a flat body
would turn each of those into a naming convention maintained by hand, and
adding a group would mean prefixing a dozen keys and hoping nobody forgets.

It also gives the PATCH a second level of partiality that a flat body
cannot express: an omitted *group* is untouched, and inside a present group
an omitted *setting* is untouched. Without it, "the client did not mention
gameplay" and "the client sent an empty gameplay object" would be the same
request — and the obvious implementation of the second is a group reset.

## Unknown keys are rejected at both levels

`extra="forbid"` from `BaseRequestDTO`, on the envelope *and* on each
group. So `{"ui": {...}}` is a `422` naming `ui`, and
`{"gameplay": {"sound": true}}` is a `422` naming `sound` — which is
A64-012.5's "reject unknown preference keys" applied where the keys
actually live. A group schema without its own `extra="forbid"` would accept
anything inside the braces, which is the failure mode the outer check
cannot see.

## Why `null` is rejected for every setting

Not one preference has an empty state: a player always has a board theme, a
language and a timezone. `null` would therefore need a meaning invented for
it — most plausibly "reset to default" — and a preferences API should not
make that decision implicitly. Omit a key to leave it alone.

## Validation lives in exactly one place per rule

The enums are the domain's (`users.domain.preferences`), so an unknown
board theme is rejected here and again in the value object, from one
definition. The timezone is checked by `validate_timezone` — the same
function `UserCreate` uses and the same one `Timezone` calls — so
A64-012.5's "timezone must be validated using the IANA database" and this
codebase's "no duplicated validation" are the same decision rather than two.
"""

from pydantic import Field, model_validator

from app.core.dto import BaseRequestDTO, BaseResponseDTO
from app.core.enums import Locale
from app.modules.users.domain.validators import validate_timezone
from app.modules.users.public import (
    DEFAULT_ANIMATION_SPEED,
    DEFAULT_BOARD_THEME,
    DEFAULT_CONFIRM_MOVE,
    DEFAULT_PIECE_SET,
    DEFAULT_SHOW_COORDINATES,
    AnimationSpeed,
    BoardTheme,
    PieceSet,
    PreferencesView,
)


def _reject_nulls[T: BaseRequestDTO](model: T) -> T:
    """Shared `model_validator` body: no key here may be sent as `null`.

    One function rather than a copy per group, because the rule is the same
    rule and there will be two more groups. It reads `model_fields_set`, so
    an *omitted* key is untouched and only an explicitly-null one is
    rejected — which is the distinction the whole PATCH design rests on.
    """
    for name in model.model_fields_set:
        if getattr(model, name) is None:
            raise ValueError(f"{name} cannot be null; send a value, or omit it to leave it alone")
    return model


class GameplayPreferencesUpdate(BaseRequestDTO):
    """The `gameplay` group of a `PATCH /profile/preferences` body.

    Every setting optional; send only what changes. Sending this group does
    **not** reset the settings inside it that you did not name.
    """

    board_theme: BoardTheme | None = Field(
        default=None,
        description=(
            "The board's colour scheme. One of `classic`, `wood`, `marble`, "
            f"`midnight`. Default: `{DEFAULT_BOARD_THEME.value}`."
        ),
        examples=[BoardTheme.WOOD],
    )
    piece_set: PieceSet | None = Field(
        default=None,
        description=(
            "Which piece artwork is drawn. One of `classic`, `modern`, `neo`. "
            f"Default: `{DEFAULT_PIECE_SET.value}`."
        ),
        examples=[PieceSet.MODERN],
    )
    confirm_move: bool | None = Field(
        default=None,
        description=(
            "Whether a move needs a second confirming action before it is sent. "
            f"Default: `{str(DEFAULT_CONFIRM_MOVE).lower()}` — a confirmation step "
            "protects against a mis-drag but costs a click on every move that was "
            "fine."
        ),
        examples=[True],
    )
    show_coordinates: bool | None = Field(
        default=None,
        description=(
            "Whether file and rank labels are drawn around the board. Default: "
            f"`{str(DEFAULT_SHOW_COORDINATES).lower()}`."
        ),
        examples=[False],
    )
    animation_speed: AnimationSpeed | None = Field(
        default=None,
        description=(
            "How fast a piece slides to its square. One of `instant`, `fast`, "
            f"`normal`, `slow`. Default: `{DEFAULT_ANIMATION_SPEED.value}`. "
            "`instant` disables motion entirely and is an accessibility setting, "
            "not a fourth speed."
        ),
        examples=[AnimationSpeed.FAST],
    )

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"examples": [{"board_theme": "wood", "confirm_move": True}]},
    }

    @model_validator(mode="after")
    def _no_nulls(self) -> "GameplayPreferencesUpdate":
        return _reject_nulls(self)


class LocalePreferencesUpdate(BaseRequestDTO):
    """The `locale` group of a `PATCH /profile/preferences` body.

    **This is the only place either of these can be changed.** Both were
    editable through `PATCH /profile` until A64-012.5, which removed them
    there — a field with two writable endpoints is a field whose validation
    and audit trail differ depending on which one a client used.
    """

    preferred_language: Locale | None = Field(
        default=None,
        description="Interface language. One of `en`, `ru`, `uz`. Default: `en`.",
        examples=[Locale.UZ],
    )
    timezone: str | None = Field(
        default=None,
        description=(
            "IANA timezone name — `Asia/Tashkent`, `Europe/London`, `UTC`. Validated "
            "against this system's IANA database. Never a UTC offset: an offset is a "
            "fact about one instant, not a timezone, and would break the moment "
            "daylight-saving rules applied. Default: `UTC`."
        ),
        examples=["Asia/Tashkent"],
    )

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"examples": [{"preferred_language": "uz"}]},
    }

    @model_validator(mode="after")
    def _no_nulls(self) -> "LocalePreferencesUpdate":
        result = _reject_nulls(self)
        # Validated here as well as in the `Timezone` value object, and it
        # is the same function in both places rather than a second rule.
        # Doing it at the boundary means the 422 names `timezone` while the
        # request is still a request; the value object remains the
        # authority for every non-HTTP caller.
        if result.timezone is not None:
            validate_timezone(result.timezone)
        return result


class PreferencesUpdateRequest(BaseRequestDTO):
    """The `PATCH /profile/preferences` body.

    Both groups optional. An omitted group is left entirely alone — this is
    what makes `{"locale": {"timezone": "UTC"}}` a timezone change rather
    than a reset of five gameplay settings.

    `{}` is legal and changes nothing, which is the honest answer to an
    empty PATCH rather than an error: the request is well formed and its
    effect is precisely none.
    """

    gameplay: GameplayPreferencesUpdate | None = Field(
        default=None,
        description="Board appearance and behaviour. Omit to leave the whole group alone.",
    )
    locale: LocalePreferencesUpdate | None = Field(
        default=None,
        description="Language and timezone. Omit to leave the whole group alone.",
    )

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [
                {"gameplay": {"board_theme": "wood", "animation_speed": "fast"}},
                {"locale": {"preferred_language": "uz", "timezone": "Asia/Tashkent"}},
                {
                    "gameplay": {
                        "board_theme": "midnight",
                        "piece_set": "neo",
                        "confirm_move": True,
                        "show_coordinates": False,
                        "animation_speed": "instant",
                    },
                    "locale": {"preferred_language": "ru", "timezone": "Europe/Moscow"},
                },
            ]
        },
    }

    @model_validator(mode="after")
    def _no_null_groups(self) -> "PreferencesUpdateRequest":
        """A group sent as `null` is a client error, like a setting sent as
        `null`. There is no "clear my gameplay preferences"; there is only
        setting them to something."""
        return _reject_nulls(self)


class GameplayPreferencesResponse(BaseResponseDTO):
    """The gameplay group, complete."""

    board_theme: BoardTheme = Field(description="The board's colour scheme.", examples=["classic"])
    piece_set: PieceSet = Field(description="Which piece artwork is drawn.", examples=["classic"])
    confirm_move: bool = Field(
        description="Whether a move needs a second confirming action.", examples=[False]
    )
    show_coordinates: bool = Field(
        description="Whether file and rank labels are drawn.", examples=[True]
    )
    animation_speed: AnimationSpeed = Field(
        description="How fast a piece slides to its square.", examples=["normal"]
    )


class LocalePreferencesResponse(BaseResponseDTO):
    """The locale group, complete."""

    preferred_language: Locale = Field(description="Your interface language.", examples=["en"])
    timezone: str = Field(description="Your IANA timezone name.", examples=["UTC"])


class PreferencesResponse(BaseResponseDTO):
    """Your preferences, as returned by both preference endpoints.

    **Always complete.** Every group is present and every setting inside it
    has a value, even for an account that has never opened a settings
    screen — the stored document is empty for such an account and the
    domain fills every absent key with its default. A client renders every
    control from this one response and never needs to know what a default
    is.

    One shape for `GET` and `PATCH`, because both answer *what are my
    settings now*, and returning the complete set from the PATCH means a
    client renders the result rather than applying an optimistic update and
    drifting from what was stored.

    **Never public.** No anonymous read path returns this or any part of
    it — see `PreferencesView`.
    """

    gameplay: GameplayPreferencesResponse
    locale: LocalePreferencesResponse

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "gameplay": {
                        "board_theme": "classic",
                        "piece_set": "classic",
                        "confirm_move": False,
                        "show_coordinates": True,
                        "animation_speed": "normal",
                    },
                    "locale": {"preferred_language": "en", "timezone": "UTC"},
                }
            ]
        }
    }

    @classmethod
    def of(cls, preferences: PreferencesView) -> "PreferencesResponse":
        """Renders the published view.

        Field by field rather than `model_validate(preferences)`, for the
        reason `users.application.mappers` gives — and with the same sharp
        edge `PrivacySettingsResponse.of` has: the DTO and this schema
        share every field name, so an implicit conversion would work today
        and would keep appearing to work on the day a sixth gameplay
        setting is added to one and not the other.
        """
        gameplay = preferences.gameplay
        locale = preferences.locale
        return cls(
            gameplay=GameplayPreferencesResponse(
                board_theme=gameplay.board_theme,
                piece_set=gameplay.piece_set,
                confirm_move=gameplay.confirm_move,
                show_coordinates=gameplay.show_coordinates,
                animation_speed=gameplay.animation_speed,
            ),
            locale=LocalePreferencesResponse(
                preferred_language=locale.preferred_language,
                timezone=locale.timezone,
            ),
        )
