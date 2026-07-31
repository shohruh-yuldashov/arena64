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
