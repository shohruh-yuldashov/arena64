"""This module's typed failures, built on the platform hierarchy in
`app.core.exceptions` (A64-006/A64-008) — never a parallel one.

Inheriting from the existing tree is what makes these work end to end with
no per-module wiring: `app/api/exception_handlers.py` maps by walking an
exception's MRO, so `UserNotFound(NotFoundError)` already returns `404`,
and `UsernameAlreadyExists(ConflictError)` already returns `409`, without
this module registering a handler or the platform learning that `users`
exists.

**Which of these carry their own wire code.** Per the rule in
`app.core.error_codes.ErrorCode`'s docstring: a class exists for every
distinct failure (that is what server code branches on), but a new *code*
is added only where a client must behave differently and the status plus
endpoint cannot tell it apart. `UserNotFound` is a `404` on a user route —
`not_found` says everything actionable. `UsernameAlreadyExists` and
`EmailAlreadyExists` are both `409` on the same request, and a sign-up form
must know which field to highlight, so those two get codes of their own.
"""

from typing import ClassVar

from app.core.error_codes import ErrorCode
from app.core.exceptions import ConflictError, NotFoundError, ValidationError


class UserNotFound(NotFoundError):
    """No user exists for the given id, username, or email.

    Carries the generic `not_found` code deliberately — see this module's
    docstring. Note that "not found" and "not permitted" must remain
    indistinguishable to an unauthorised caller once `auth` lands
    (services.md §6 Tier 2's enumeration-oracle rule); keeping this on the
    generic code means that convergence needs no wire change.
    """


class UsernameAlreadyExists(ConflictError):
    """The username is taken, compared case-insensitively (UP-1)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.USERNAME_ALREADY_EXISTS


class EmailAlreadyExists(ConflictError):
    """The email is registered, compared on its normalised form (AC-1)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.EMAIL_ALREADY_EXISTS


class InvalidUsername(ValidationError):
    """Shape, length, or reserved-name rule violated — see
    `domain/validators.py::validate_username`."""

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_USERNAME


class InvalidEmail(ValidationError):
    """Structurally not an address — see `domain/validators.py::validate_email`."""

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_EMAIL


class InvalidLanguage(ValidationError):
    """Not one of the platform's supported locales (`app.core.enums.Locale`)."""


class InvalidTimezone(ValidationError):
    """Not an IANA timezone name known to this system."""


# --- presentational identity (A64-012.1) -------------------------------------
#
# Neither carries a wire code of its own, and that is the rule in
# `app.core.error_codes.ErrorCode` applied rather than an omission: a code
# is earned when a client must *behave* differently and the status plus the
# endpoint cannot say which field was wrong. Both of these are only ever
# reachable from a future profile-edit form, which submits several fields
# at once — so when that endpoint exists they will need codes, and adding
# them is the edit task's to do alongside the form that needs them.


class InvalidDisplayName(ValidationError):
    """Too short, too long, or carrying characters a rendered name must
    not — see `domain/validators.py::validate_display_name`."""


class InvalidBio(ValidationError):
    """Too long, or carrying characters a plain-text field must not —
    see `domain/validators.py::validate_bio`."""


class InvalidCountryCode(ValidationError):
    """Not a two-letter ISO 3166-1 alpha-2 code."""


class InvalidSearchTerm(ValidationError):
    """The search term is empty, out of bounds, a wildcard attempt, or
    carries nothing that could identify a player — A64-013.1.

    Carries no `default_code`, so it lands on the platform's generic
    `validation_error`. **Deliberate**: a distinct code per rejection reason
    would let a caller enumerate the input filter itself, and every one of
    these is the same answer to the client — *fix the term* — with a
    message that already says which rule fired. See `domain/search.py` for
    the rules and their ordering.
    """


class InvalidSearchCursor(ValidationError):
    """The pagination cursor is malformed, or belongs to a different search
    term — A64-013.1.

    The second case is the one worth having a type for. A search cursor
    encodes a *rank* computed against one particular term, so replaying it
    against another term would resume at an offset that means nothing and
    silently skip or repeat people. Rejecting is the only honest outcome,
    and it is a client error rather than an empty page.
    """


class InvalidPreference(ValidationError):
    """A stored preference document holds a value this application cannot
    have written — A64-012.5.

    Raised only when *reading* (`GameplayPreferences.from_document`), never
    from the HTTP boundary: an unknown key or a bad enum value in a request
    is rejected by the request schema before it reaches the domain, with a
    422 naming the field. Reaching this exception therefore means the
    `jsonb` column was written by something other than this code path, and
    the honest answer is to fail rather than silently reset a player's
    settings to defaults.

    A `ValidationError` rather than a new kind, so the existing handler
    table maps it without a new entry — a 422 is not quite the right shape
    for stored corruption, and inventing a fifth status for a case that
    should never occur would be worse than the imprecision.
    """


__all__ = [
    "EmailAlreadyExists",
    "InvalidBio",
    "InvalidCountryCode",
    "InvalidDisplayName",
    "InvalidEmail",
    "InvalidPreference",
    "InvalidSearchCursor",
    "InvalidSearchTerm",
    "InvalidLanguage",
    "InvalidTimezone",
    "InvalidUsername",
    "UserNotFound",
    "UsernameAlreadyExists",
]
