"""Value objects — immutable, self-validating, compared by value.

Each wraps a `str` that has *already been proven valid at construction*,
so nothing downstream needs to re-check it. That is the property that
makes them worth the wrapper: a function taking `username: str` can be
handed any string in the program, while one taking `username: Username`
can only be handed something that went through `validate_username`.
domain-model.md DM-01 is the general rule — no identity, interchangeable
when equal, therefore a value and not an entity.

`frozen=True` is load-bearing, not decoration: a mutable value object can
be changed after validation, which defeats the entire construct-once
guarantee above.
"""

from dataclasses import dataclass

from app.modules.users.domain.validators import (
    fold_username,
    validate_bio,
    validate_country_code,
    validate_display_name,
    validate_email,
    validate_timezone,
    validate_username,
)


@dataclass(frozen=True, slots=True)
class Username:
    """A player's chosen handle, in the casing they chose it.

    `value` preserves capitalisation for display; `folded` is the
    comparison form the uniqueness rule uses (UP-1). Both live here so that
    no caller ever has to remember to fold — and so a caller that compares
    two `Username`s with `==` gets the *case-sensitive* answer, which is
    almost never what uniqueness means; `folded` is explicit about it.
    """

    value: str

    def __post_init__(self) -> None:
        validate_username(self.value)

    @property
    def folded(self) -> str:
        return fold_username(self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Email:
    """A normalised email address.

    Unlike `Username` there is no separate display form — `validate_email`
    returns the normalised value and that is what is stored. See its
    docstring for why one form rather than two.
    """

    value: str

    def __post_init__(self) -> None:
        # Frozen dataclass: assignment goes through object.__setattr__.
        # Normalising here rather than asking every caller to pre-normalise
        # is what makes `Email("  A@B.COM ") == Email("a@b.com")` true,
        # which is the only sane meaning of equality for an address.
        object.__setattr__(self, "value", validate_email(self.value))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Timezone:
    """An IANA timezone name — `Europe/London`, never a `+01:00` offset.
    See `validators.validate_timezone` for why the distinction matters."""

    value: str

    def __post_init__(self) -> None:
        validate_timezone(self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Bio:
    """A player's self-description — plain text, bounded, inert.

    Normalises on construction like `Email` rather than merely validating
    like `Username`, because the stored form *is* the trimmed form: a bio
    that differs only by trailing whitespace is the same bio, and keeping
    both would make the length bound depend on invisible characters.

    There is deliberately no `is_empty` or `or_none` helper. "No bio" is
    `None` at the field, not an empty `Bio` — one absent state rather than
    two that every renderer would have to check separately. Constructing
    `Bio("")` is legal and yields an empty value; it is the *entity* that
    decides an empty string means absence, and it does so in one place.
    """

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", validate_bio(self.value))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class CountryCode:
    """An ISO 3166-1 alpha-2 country code, upper-cased.

    Validated for *shape* only — see `validate_country_code` on why
    membership belongs to the `reference.country` table rather than to a
    constant in this file.

    A value object rather than a bare `str` for the reason the module
    docstring gives: a function taking `CountryCode` cannot be handed
    `"United Kingdom"`, `"gbr"`, or a display name someone read off a form.
    """

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", validate_country_code(self.value))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class DisplayName:
    """The free-form name a player renders under.

    Normalises on construction like `Email` and `Bio` — the stored form is
    the trimmed form, so a name differing only by a trailing space is the
    same name rather than a second one that sorts differently.

    A value object rather than a bare `str` from A64-012.3 onward, and the
    reason is the field becoming *editable*: until then nothing but
    registration wrote it, and `User.display_name` was a plain string
    nobody could put a control character into. An endpoint that accepts one
    from a client every day needs the guarantee at the type, not at the one
    call site somebody remembered.

    Distinct from `Username`: a handle is unique, folded, ASCII-restricted
    and part of a URL; this is decorative, Unicode-friendly and shared by
    as many players as like it. Conflating them is how a display name ends
    up in a route.
    """

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", validate_display_name(self.value))

    def __str__(self) -> str:
        return self.value
