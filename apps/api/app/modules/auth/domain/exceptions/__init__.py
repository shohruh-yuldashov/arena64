"""This module's typed failures, on the platform hierarchy in
`app.core.exceptions` — never a parallel one.

Inheriting from the existing tree is what makes these work end to end
with no per-module wiring: `app/api/exception_handlers.py` maps by walking
an exception's MRO, so `WeakPassword(ValidationError)` already returns
`422` without `auth` registering a handler.
"""

from typing import ClassVar

from app.core.error_codes import ErrorCode
from app.core.exceptions import ValidationError


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


__all__ = ["WeakPassword"]
