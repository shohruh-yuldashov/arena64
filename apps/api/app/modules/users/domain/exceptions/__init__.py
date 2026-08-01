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


class InvalidBio(ValidationError):
    """Too long, or carrying characters a plain-text field must not —
    see `domain/validators.py::validate_bio`."""


class InvalidCountryCode(ValidationError):
    """Not a two-letter ISO 3166-1 alpha-2 code."""


__all__ = [
    "EmailAlreadyExists",
    "InvalidBio",
    "InvalidCountryCode",
    "InvalidEmail",
    "InvalidLanguage",
    "InvalidTimezone",
    "InvalidUsername",
    "UserNotFound",
    "UsernameAlreadyExists",
]
