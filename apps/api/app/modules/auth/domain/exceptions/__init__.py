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


# --- bearer tokens (A64-011.3) ----------------------------------------------
#
# All five are `AuthenticationFailed`, so all five are 401 through the
# existing MRO walk with no new handler. They form a small tree rather
# than five siblings, because callers genuinely branch at two different
# granularities: a route only ever cares "was a usable identity proven",
# while the token plumbing and its tests care exactly which check failed.
#
#     AuthenticationFailed
#     +-- AuthenticationRequired    no usable credential was presented
#     |   +-- MissingToken           ... specifically, none at all
#     +-- InvalidToken             a credential was presented and refused
#         +-- ExpiredToken           ... because it aged out
#         +-- InvalidSignature       ... because it was not signed by us


class AuthenticationRequired(AuthenticationFailed):
    """This endpoint needs a proven identity and does not have one — 401.

    The general case: `get_current_user` reached the end without a
    principal. Distinct from `InvalidToken` in the one way a client acts
    on: there is nothing stored to discard, so the correct response is to
    prompt for sign-in rather than to clear a token and *then* prompt.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.AUTHENTICATION_REQUIRED


class MissingToken(AuthenticationRequired):
    """No `Authorization: Bearer` header was sent at all — 401.

    A subclass rather than a sibling because it is the specific reason for
    the general condition above, and because a route guard written against
    `AuthenticationRequired` must keep catching it if a later task adds a
    second way to be unauthenticated (a cookie, a WebSocket ticket).

    Carries the same wire code as its parent on purpose: a client that
    sent no credential learns nothing from being told which of the several
    places it could have sent one were checked.
    """


class InvalidToken(AuthenticationFailed):
    """A token was presented and cannot be trusted — 401.

    The catch-all for every structural failure: not three dot-separated
    segments, not valid base64, not valid JSON, missing a required claim,
    a claim of the wrong type, the wrong `iss`, the wrong `aud`, or the
    wrong `type`.

    **Every one of those produces this same exception and the same
    message.** Reporting *which* check failed would hand anyone probing
    the token format a step-by-step oracle: change one field, see whether
    the complaint moves on to the next check, and learn the exact shape of
    a token the platform would accept. The server knows precisely what
    went wrong and says so in its logs, at DEBUG, where the caller cannot
    read it.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_TOKEN


class ExpiredToken(InvalidToken):
    """The token was ours, was well-formed, and has passed its `exp` — 401.

    The one token failure that gets its own wire code, because it is the
    one a client must handle differently: an expired access token means
    *refresh and retry* (A64-011.4), not *sign in again*. Treating it like
    any other invalid token would sign a user out every fifteen minutes,
    which is the failure mode refresh tokens exist to prevent.

    Disclosing expiry is not a leak in the way the other cases would be.
    The client already holds the token and can read its `exp` itself —
    the payload is base64, not encrypted — so the server is confirming
    something the caller can compute, not revealing something it cannot.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.EXPIRED_TOKEN


class InvalidSignature(InvalidToken):
    """The token did not verify under any active signing key — 401.

    A subclass so that the platform can *log* a forgery distinctly (this
    is the one token failure that is never an accident, and its rate is
    worth alerting on) while returning the parent's `invalid_token` code,
    which tells a forger nothing about how close they got.

    "Any active key" includes `JWTSettings.previous_secret_keys`, so a
    token signed just before a key rotation is not a forgery.
    """


# --- refresh sessions (A64-011.4) --------------------------------------------
#
# All four are `AuthenticationFailed`, so all four are 401 through the
# existing MRO walk with no new handler. They form a tree rather than four
# siblings because callers branch at two granularities: a refresh endpoint
# only ever asks "may this token be exchanged", while the service and its
# tests care exactly which check failed.
#
#     AuthenticationFailed
#     +-- InvalidRefreshToken       the token cannot be exchanged
#         +-- ExpiredRefreshToken     ... it aged out or sat idle
#         +-- RevokedSession          ... its session was revoked
#         +-- SessionNotFound         ... no session matches it


class InvalidRefreshToken(AuthenticationFailed):
    """The presented refresh token cannot be exchanged — 401.

    The catch-all, and the parent of the three specific reasons below so
    that a caller can catch one type and be sure it has covered all of
    them. A refresh endpoint that caught only `ExpiredRefreshToken` and
    let a `RevokedSession` escape as a 500 is the bug this hierarchy
    prevents.

    The message is a fixed string for every case. The server knows which
    check failed and records it at DEBUG; the caller does not, because a
    finer answer tells whoever presented the token whether it was ever
    real — see `app.core.error_codes.ErrorCode` on why that is a
    membership oracle over the session table.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_SESSION


class ExpiredRefreshToken(InvalidRefreshToken):
    """The session aged out — 401.

    Covers both expiries database.md §4.4 requires: the absolute 30-day
    window and the idle window. They are one exception because the client
    does the same thing for both — sign in again — and because telling a
    caller *which* window elapsed reveals when the session was last used,
    which is information about the legitimate user that only an attacker
    would need.

    The one refresh failure with its own wire code, because it is the one
    a UI can explain rather than merely report: "your session expired,
    please sign in" is actionable, and it is true rather than alarming.
    Disclosing it is safe for the same reason expiry disclosure is safe
    generally — the client can observe that it has not used the token in
    a month without being told.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.SESSION_EXPIRED


class RevokedSession(InvalidRefreshToken):
    """The session was revoked — 401.

    A subclass so the platform can *log* a revoked-token presentation
    distinctly. That signal matters: a token presented after revocation
    is either a client that has not noticed it was signed out, or the
    holder of a stolen credential discovering the session is gone. The
    rate of these is worth watching even though the response is
    indistinguishable from any other rejection.

    Carries the parent's `invalid_session` code deliberately — see
    `InvalidRefreshToken`.
    """


class SessionNotFound(InvalidRefreshToken):
    """No session matches the presented token — 401.

    Note what this is *not*: it is not a `NotFoundError`, and so it is not
    a 404. A 404 would confirm that the endpoint looked something up and
    did not find it, which over a session table is exactly the membership
    oracle this hierarchy avoids. Every refresh failure is 401 —
    "I do not know who you are" — regardless of why.
    """


__all__ = [
    "AccountLocked",
    "AuthenticationRequired",
    "ExpiredRefreshToken",
    "ExpiredToken",
    "InactiveAccount",
    "InvalidCredentials",
    "InvalidRefreshToken",
    "InvalidSignature",
    "InvalidToken",
    "MissingToken",
    "RevokedSession",
    "SessionNotFound",
    "WeakPassword",
]
