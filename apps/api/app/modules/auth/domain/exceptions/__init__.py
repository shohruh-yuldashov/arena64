"""This module's typed failures, on the platform hierarchy in
`app.core.exceptions` — never a parallel one.

Inheriting from the existing tree is what makes these work end to end
with no per-module wiring: `app/api/exception_handlers.py` maps by walking
an exception's MRO, so `WeakPassword(ValidationError)` already returns
`422` without `auth` registering a handler.
"""

from typing import ClassVar

from app.core.error_codes import ErrorCode
from app.core.exceptions import (
    AuthenticationFailed,
    PermissionDeniedError,
    ValidationError,
)


class WeakPassword(ValidationError):
    """The password does not meet the policy in `domain/validators.py`.

    Carries its own wire code because a registration form submits three
    fields at once and a bare `validation_error` gives a client no way to
    know which input to mark — the rule for earning a code, stated in
    `app.core.error_codes.ErrorCode`.

    **The message never contains the password, or any part of it.** It
    describes the *rule* that was not met ("must contain an uppercase
    letter"), never the value. An error string is the single most common
    way a credential reaches a log, a screenshot, or a bug report
    (services.md §8.5), and this is the one exception on the platform
    where that would be a disclosure rather than an annoyance.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.WEAK_PASSWORD


class InvalidCredentials(AuthenticationFailed):
    """The submitted email and password do not identify anyone — 401.

    **Deliberately ambiguous, and the ambiguity is the feature.** This is
    raised identically when no account has that address and when the
    address exists but the password is wrong. A caller cannot tell the two
    apart from the code, the message, the status, or (see
    `AuthenticationService`) the elapsed time.

    That is not caution for its own sake. An endpoint that distinguishes
    them is a membership oracle for any address an attacker cares to
    submit, and "does this person have an account on this site" is
    disclosure on its own — worse, it turns a credential-stuffing list
    into a *targeted* one, since only the addresses that exist are worth
    spending guesses on.

    The message is a fixed string. Never interpolate the submitted address
    into it: an error message is a place personal data reaches logs and
    screenshots (services.md §8.5), and here it would also undo the
    ambiguity above by proving the server read the value.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_CREDENTIALS


class InactiveAccount(PermissionDeniedError):
    """The credentials were correct, but the account is deactivated — 403.

    403, not 401: the caller *did* prove who they are (RFC 9110 §11.6.1 —
    401 means "I do not know who you are"). Re-authenticating would change
    nothing, and a client that saw 401 would be right to prompt for the
    password again, which is exactly the wrong instruction to give
    someone whose password was correct.

    Distinguishable from `InvalidCredentials` only *after* a successful
    verification, which is why it is not the enumeration oracle it looks
    like — see `AuthenticationService`. A caller who reaches this already
    knows the password; being told the account is disabled tells them
    nothing they could not confirm anyway, and withholding it would leave
    them retyping a correct password forever.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.INACTIVE_ACCOUNT


class AccountLocked(PermissionDeniedError):
    """The credentials were correct, but sign-in is temporarily barred —
    403.

    Separate from `InactiveAccount` because the client's correct response
    genuinely differs: a lock lapses on its own and the right advice is
    "try again later", while a deactivation does not and the right advice
    is "contact support". Two codes because two behaviours, which is the
    test `app.core.error_codes.ErrorCode` sets.

    Reached only after a successful verification, exactly as
    `InactiveAccount` is.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.ACCOUNT_LOCKED


__all__ = [
    "AccountLocked",
    "InactiveAccount",
    "InvalidCredentials",
    "WeakPassword",
]
