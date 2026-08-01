"""`Preferences` — the personal settings that shape a player's own
experience, in the groups domain-model.md §7.1 names.

Framework-free like the rest of `domain/` (architecture.md §8). No
SQLAlchemy, no Pydantic, no JSON encoder — `as_document`/`from_document`
below speak plain dictionaries and the infrastructure layer decides what to
do with them.

## The four groups, and the two that exist

domain-model.md §7.1: "Own preferences in four groups: **gameplay** (board
theme, piece set, premove, auto-promote, confirm-move), **privacy**
(profile visibility, who may challenge, who may see online status, who may
direct-message), **notifications** (per-event, per-channel), **locale**
(language, timezone, time-and-date format, board orientation)."

    gameplay        here, A64-012.5
    locale          here, A64-012.5
    privacy         `domain/privacy.py`, A64-012.4 — a separate type, see
                    below
    notifications   not built. Explicitly out of A64-012.5's scope, and
                    correctly so: database.md §4.9 is emphatic that
                    notification preferences are a *relation* keyed
                    `(player, category, channel)` rather than anything
                    document-shaped, because the dispatcher asks "is this
                    player opted in to this category on this channel" as an
                    index probe at event rate.

**Privacy is deliberately not folded in here**, even though §7.1 lists it
as a fourth group of the same kind. It has its own type, its own port, its
own endpoint and its own rate limit already, and the reason is the one
A64-012.4 records: a preference decides what *you* see, a privacy flag
decides what *strangers* see. Merging them would mean a component that may
change a board theme could also publish an account's activity. The grouping
in the design document is about what a profile *owns*, not about what a
single write should be able to reach.

## Why `None` means "unchanged" in every `updated()` below

The same argument `PrivacySettings` makes, and it holds for exactly the
same reason: not one field in this file is nullable. A player always has a
board theme, always has a language, always has a timezone — there is no
"clear my animation speed" — so `None` is free to mean "leave it alone"
without colliding with a real value. `app.core.sentinels.UNSET` is what a
nullable field needs, and is what `ProfileEdits` uses for the three
profile fields that genuinely have an empty state.

## Why the enums are closed

`board_theme` and `piece_set` are, as database.md §4.8 puts it,
"client-rendered themes" — the backend stores a name and never draws
anything. That is an argument for `text`, and the counter-argument is
A64-012.5's "validate enum values": an unvalidated theme name is a column
that accumulates typos from every client version ever shipped, and no
amount of frontend care fixes a value already stored.

Closed enums win here because the set is small and platform-owned — the
task's constraints exclude a theme marketplace, so nobody outside this
repository is adding one. The cost is real and worth naming: shipping a new
board theme is a backend change (an enum member, and nothing else — the
column is `jsonb`, so no migration). If a marketplace ever arrives, these
become `reference.board_theme` rows and the validator becomes a lookup,
which is the same path `validate_country_code` is on.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.enums import Locale
from app.modules.users.domain.exceptions import InvalidPreference
from app.modules.users.domain.value_objects import Timezone


class BoardTheme(StrEnum):
    """The board's colour scheme. Rendered entirely by the client."""

    CLASSIC = "classic"
    WOOD = "wood"
    MARBLE = "marble"
    MIDNIGHT = "midnight"


class PieceSet(StrEnum):
    """Which piece artwork the client draws."""

    CLASSIC = "classic"
    MODERN = "modern"
    NEO = "neo"


class AnimationSpeed(StrEnum):
    """How fast a piece slides to its square.

    `INSTANT` is a real accessibility setting rather than a fourth speed:
    motion is a migraine and vestibular trigger, and a player who needs it
    off needs it off rather than fast. Spelled as a member of this enum
    instead of a separate `animations_enabled` boolean so a client renders
    one control, and so "off" cannot disagree with a speed set beside it.
    """

    INSTANT = "instant"
    FAST = "fast"
    NORMAL = "normal"
    SLOW = "slow"


#: The defaults A64-012.5 asks for ("provide sensible defaults"), named so
#: that the migration's `server_default`, the value object and the OpenAPI
#: documentation cannot disagree about them.
#:
#: `confirm_move` is off and `show_coordinates` is on, which is the pairing
#: worth explaining. A confirmation step on every move is protection against
#: a mis-drag that costs a game, and it is also an extra click on every one
#: of the forty moves that were fine — most players want it off, and the
#: ones who want it on will find it. Coordinates are the opposite: they cost
#: nothing to display, they are how a beginner learns to read a board, and
#: a strong player who finds them noisy turns them off once.
DEFAULT_BOARD_THEME = BoardTheme.CLASSIC
DEFAULT_PIECE_SET = PieceSet.CLASSIC
DEFAULT_CONFIRM_MOVE = False
DEFAULT_SHOW_COORDINATES = True
DEFAULT_ANIMATION_SPEED = AnimationSpeed.NORMAL


@dataclass(frozen=True, slots=True)
class GameplayPreferences:
    """How the board behaves and looks for one player.

    Frozen, like every other settings type on the platform: a preference a
    renderer could quietly rewrite while drawing a board is not a
    preference. `updated()` returns a new value and the use case assigns
    it, so the only writer is the one that meant to write.

    Nothing here affects the *rules*. A confirmation step and an animation
    speed are client behaviour; legality, clocks and results are the game
    module's and are not negotiable per player (database.md §4.8 keeps rule
    variations as columns on a match precisely so they stay auditable).
    """

    board_theme: BoardTheme = DEFAULT_BOARD_THEME
    piece_set: PieceSet = DEFAULT_PIECE_SET
    confirm_move: bool = DEFAULT_CONFIRM_MOVE
    """Whether a move needs a second confirming action before it is sent."""

    show_coordinates: bool = DEFAULT_SHOW_COORDINATES
    """Whether file and rank labels are drawn around the board."""

    animation_speed: AnimationSpeed = DEFAULT_ANIMATION_SPEED

    def updated(
        self,
        *,
        board_theme: BoardTheme | None = None,
        piece_set: PieceSet | None = None,
        confirm_move: bool | None = None,
        show_coordinates: bool | None = None,
        animation_speed: AnimationSpeed | None = None,
    ) -> "GameplayPreferences":
        """A copy with the named settings replaced; `None` leaves one alone.

        Keyword-only and explicitly typed rather than `**kwargs` into
        `dataclasses.replace`, for the reason `PrivacySettings.updated`
        gives: a misspelled setting must be a type error, not a silently
        ignored key.
        """
        return GameplayPreferences(
            board_theme=self.board_theme if board_theme is None else board_theme,
            piece_set=self.piece_set if piece_set is None else piece_set,
            confirm_move=self.confirm_move if confirm_move is None else confirm_move,
            show_coordinates=(
                self.show_coordinates if show_coordinates is None else show_coordinates
            ),
            animation_speed=self.animation_speed if animation_speed is None else animation_speed,
        )

    def as_document(self) -> dict[str, Any]:
        """The `jsonb` form — plain strings and booleans, no enum objects.

        Writes **every** key, even ones still at their default. The
        alternative (storing only what the player changed) would let a
        later change to `DEFAULT_ANIMATION_SPEED` retroactively alter the
        experience of somebody who had deliberately chosen the old default
        and could not tell the difference between "I chose this" and "I
        never looked".

        The empty document is still legal and is what a new account has —
        see `from_document`. So a default change *does* reach everyone who
        has never opened the settings screen, which is the group it should
        reach.
        """
        return {
            "board_theme": self.board_theme.value,
            "piece_set": self.piece_set.value,
            "confirm_move": self.confirm_move,
            "show_coordinates": self.show_coordinates,
            "animation_speed": self.animation_speed.value,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any] | None) -> "GameplayPreferences":
        """Reads the `jsonb` form, defaulting anything absent.

        **A missing key is a default, not an error**, and that is the whole
        reason this column can be `jsonb` without a migration per setting:
        a sixth preference added next quarter reads as its default for
        every row already stored, with no backfill and no `NOT NULL`
        violation. `{}` — what every account starts with — reads as the
        complete platform defaults.

        An *unknown* key is ignored rather than rejected. Rejecting is the
        HTTP boundary's job (`extra="forbid"`, A64-012.5's "reject unknown
        preference keys"), and it has already happened by the time anything
        is written. Here the input is a row this application wrote, and
        refusing to load a profile because a *removed* setting is still in
        the document would turn deleting a preference into an outage —
        database.md RK-9's "consumers validate on read" cuts both ways.

        A key that is present but **malformed** — a board theme that is not
        a board theme — does raise. That is data this application cannot
        have written, so it is corruption rather than history, and reading
        past it would silently reset somebody's settings.
        """
        if not document:
            return cls()

        return cls(
            board_theme=_enum_member(BoardTheme, document, "board_theme", DEFAULT_BOARD_THEME),
            piece_set=_enum_member(PieceSet, document, "piece_set", DEFAULT_PIECE_SET),
            confirm_move=_boolean(document, "confirm_move", DEFAULT_CONFIRM_MOVE),
            show_coordinates=_boolean(document, "show_coordinates", DEFAULT_SHOW_COORDINATES),
            animation_speed=_enum_member(
                AnimationSpeed, document, "animation_speed", DEFAULT_ANIMATION_SPEED
            ),
        )


@dataclass(frozen=True, slots=True)
class LocalePreferences:
    """The language a player reads and the timezone their times are shown
    in.

    Both were columns read straight off `User` until A64-012.5; they are
    grouped here because they are preferences by every definition the
    design documents use, and because leaving them addressable from two
    endpoints was the duplicated writable surface that task set out to
    remove.

    `timezone` is the existing `Timezone` value object, not a `str`, so the
    IANA check happens on construction and happens identically whether the
    value arrives from HTTP, from a repository row, or from a test — which
    is A64-012.5's "timezone must be validated using the IANA database"
    and this codebase's "no duplicated validation" in one decision.

    Deliberately **not** carrying a date format or a board orientation,
    which §7.1 also lists under locale. Neither is in A64-012.5's scope and
    neither has a consumer; adding them now would be shape invented ahead
    of a caller (CLAUDE.md §1 rule 7).
    """

    preferred_language: Locale = Locale.EN
    timezone: Timezone = field(default_factory=lambda: Timezone("UTC"))
    """`UTC` by default. A `default_factory` rather than a shared constant
    instance only because `Timezone` validates on construction and a
    module-level instance would run that validation at import time."""

    def updated(
        self,
        *,
        preferred_language: Locale | None = None,
        timezone: Timezone | None = None,
    ) -> "LocalePreferences":
        return LocalePreferences(
            preferred_language=(
                self.preferred_language if preferred_language is None else preferred_language
            ),
            timezone=self.timezone if timezone is None else timezone,
        )


@dataclass(frozen=True, slots=True)
class Preferences:
    """One player's personal settings, grouped as domain-model.md §7.1
    groups them.

    A composite rather than eight loose fields, and the grouping is load
    bearing rather than tidy: it is the unit the API patches
    (`{"gameplay": {...}}`), the unit the log records ("updated groups"),
    and the unit a fourth group is added as. Flattening it would make
    "which group did this request touch" a question nobody can answer
    without a list of field names kept in step by hand.
    """

    gameplay: GameplayPreferences = GameplayPreferences()
    locale: LocalePreferences = field(default_factory=LocalePreferences)


def _enum_member[T: StrEnum](
    enum_cls: type[T], document: Mapping[str, Any], key: str, default: T
) -> T:
    """One `jsonb` value as an enum member, defaulting when absent.

    Raises `InvalidPreference` for a value that is present and not a member
    — see `GameplayPreferences.from_document` on why absent and malformed
    are treated differently.
    """
    if key not in document:
        return default
    try:
        return enum_cls(document[key])
    except ValueError as error:
        raise InvalidPreference(f"Stored {key} is not a recognised value.") from error


def _boolean(document: Mapping[str, Any], key: str, default: bool) -> bool:
    """One `jsonb` value as a boolean, with no coercion.

    `isinstance` rather than `bool(...)`, because `bool("false")` is `True`
    and a stored string would silently invert a setting. JSON has a real
    boolean; anything else in this position is corruption.
    """
    if key not in document:
        return default
    value = document[key]
    if not isinstance(value, bool):
        raise InvalidPreference(f"Stored {key} is not a boolean.")
    return value
