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


__all__ = [
    "EmailAlreadyExists",
    "InvalidEmail",
    "InvalidLanguage",
    "InvalidTimezone",
    "InvalidUsername",
    "UserNotFound",
    "UsernameAlreadyExists",
]
