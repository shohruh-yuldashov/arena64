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
    ConflictError,
    PermissionDeniedError,
    TemporaryConflictError,
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


class AccountRestricted(PermissionDeniedError):
    """The account is under an administrative restriction — 403. A64-024.6.

    403 for `InactiveAccount`'s reason: the caller proved who they are, and
    re-authenticating would change nothing.

    **Distinct from `InactiveAccount`**, which the account's own owner
    caused. `domain-model.md` §6 draws the two transitions separately —
    "Active → Suspended: sanction applied" against "Active → Deactivated:
    player-initiated" — and collapsing them here would make the platform
    unable to tell a departure from a removal at the one moment it matters.

    The **message** is deliberately the same shape for both: what a
    restricted person is told is a product decision recorded in
    `specs/admin.md`, and nothing about the category, the reasoning, the
    case or the administrator who decided may reach a client from here.
    """


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

    The general case. Distinct from `InvalidToken` in the one way a client
    acts on: there is nothing stored to discard, so the correct response is
    to prompt for sign-in rather than to clear a token and *then* prompt.

    **Nothing raises this directly**, and that is intentional rather than
    an oversight — it is the parent `MissingToken` specialises, so a route
    guard written against this type keeps working when a later task adds a
    second way to be unauthenticated (a cookie, a WebSocket ticket).
    A64-011.9 removed its one direct raiser, a `request.state` reader that
    nothing populated; see `presentation/dependencies/current_user.py` for
    why that function should not have existed.

    A parent with no direct raiser is not the thing this module refuses
    elsewhere. `EmailAlreadyVerified` was deleted because no code path
    could *reach* it, so `except EmailAlreadyVerified` was dead; this type
    is reached on every request that arrives without a bearer token,
    through its subclass.
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


# --- email verification (A64-011.6) ------------------------------------------


class InvalidVerificationToken(ValidationError):
    """The verification link cannot be redeemed — 422.

    Covers every unusable case: no such token, already used, and expired.
    One exception and one message for all three, deliberately — the
    client's action is identical (request a new link), and telling a
    caller *which* it was reports on whether a token they hold was ever
    real. The server records the distinction at DEBUG, where a caller
    cannot read it.

    A `ValidationError` (422) rather than an `AuthenticationFailed` (401),
    and the distinction is not cosmetic: this endpoint is not
    authenticated and is not *about* identity. A 401 would tell a client
    to prompt for sign-in, which is exactly the wrong instruction for
    someone who clicked a stale link in an email — the fix is a new link,
    not a password.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_VERIFICATION_TOKEN


# --- the six-digit code (A64-021.5H) ----------------------------------------
#
# Four exceptions where the link path has one, and the asymmetry is the
# difference between the two credentials rather than an inconsistency.
#
# A link is a 32-byte random value: unknown, used and expired are one answer
# because a caller can do nothing differently and distinguishing them reports
# on whether a token they hold was ever real. A code is six digits typed by
# somebody who **is already authenticated as this account**, so there is no
# account to enumerate and every distinction below is one the person needs:
# retype it, ask for another, or wait.


class InvalidVerificationCode(ValidationError):
    """The code is wrong, malformed, or there is no challenge — 422.

    Three causes, one answer, and here that *is* the right collapse: all
    three mean "this did not work, type the current code". Splitting "wrong"
    from "no challenge outstanding" would tell a caller whether an account
    has one open, which is the one thing this endpoint should not report.

    Never says how close a guess was — §9.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.EMAIL_VERIFICATION_CODE_INVALID


class VerificationCodeExpired(ValidationError):
    """The ten-minute window elapsed — 422.

    Its own code because the recovery genuinely differs: retyping is
    pointless and the client should offer a resend rather than the field
    again. That distinction is safe to make for an authenticated caller and
    would not be for the anonymous link endpoint.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.EMAIL_VERIFICATION_CODE_EXPIRED


class VerificationAttemptsExceeded(ValidationError):
    """Five wrong codes — 422, and the challenge is gone.

    Distinct from expiry because the *cause* is different and so is what a
    client should say: waiting does not help, and a new code is required.
    Distinct from a plain refusal because the state changed — nothing about
    the old challenge will work again.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.EMAIL_VERIFICATION_ATTEMPTS_EXCEEDED


class VerificationResendTooSoon(TemporaryConflictError):
    """A code was sent less than a minute ago — 409, with `Retry-After`.

    A `TemporaryConflictError` rather than a rate limit, and the platform
    already made this exact distinction for the matchmaking decline
    cooldown: *"the platform's state refused the request, and the caller did
    nothing wrong"*. No budget was spent — a code exists and is still valid,
    which is why another one is refused.

    Choosing the existing type also means the transport rendering is
    already written: `exception_handlers` emits `Retry-After` for anything
    that is one of these, without knowing which module raised it.

    The interval is measured from the **stored challenge**, never from the
    configured window, for the reason that class gives about extending
    cooldowns — a durable row is what a reload, a second tab and a second
    node all agree about (§11, §22).
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.EMAIL_VERIFICATION_RESEND_TOO_SOON


class EmailAlreadyVerified(ConflictError):
    """The address is already confirmed — 409.

    Only ever raised on **resend**, never on verify: §23 makes a code
    submitted after another tab succeeded an idempotent success, because
    the person did the right thing and the outcome they wanted is true.
    Asking for another code when there is nothing to verify is a different
    situation — nothing would be sent, and saying so is more useful than a
    silent acceptance.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.EMAIL_ALREADY_VERIFIED


class EmailVerificationRequired(PermissionDeniedError):
    """This action needs a verified address — 403.

    The one exception the *product* surfaces rather than the verification
    flow: raised by `RequireVerifiedEmail` on a write an unverified account
    attempted. `403` rather than `401`, and the distinction is a client
    behaviour — the caller is authenticated and re-authenticating would
    change nothing. The fix is `/verify-email`, and the stable code is what
    tells a client to go there.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.EMAIL_VERIFICATION_REQUIRED


# --- password reset (A64-011.7) ----------------------------------------------


class InvalidResetToken(ValidationError):
    """The password-reset link cannot be redeemed — 422.

    Covers every unusable case: no such token, already used, and expired.
    One exception and one message for all three, deliberately — the
    client's action is identical (ask for a new link), and telling a caller
    *which* it was reports on whether a token they hold was ever real. The
    server records the distinction in its logs, where a caller cannot read
    it.

    A `ValidationError` (422) rather than an `AuthenticationFailed` (401),
    for the reason `InvalidVerificationToken` gives and one more. The
    shared reason: this endpoint is unauthenticated and is not *about*
    identity, so a 401 would tell a client to prompt for sign-in — which
    is precisely the wrong instruction for someone who is here because
    they cannot sign in. The additional one: a 401 on this endpoint would
    also be the only 401 on the platform that a correct client should
    respond to by *not* re-authenticating, and that contradiction is how a
    generic HTTP interceptor ends up bouncing a person out of the recovery
    flow that was working.

    Note what is deliberately **not** here: no `PasswordResetNotAllowed`,
    no `AccountNotEligibleForReset`. The forgot-password path decides
    silently and reveals nothing (see `PasswordResetService`), so there is
    no caller for such a type — and an exception with no raiser on a
    security surface reads as "this case is handled" to whoever adds the
    next endpoint.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_RESET_TOKEN


__all__ = [
    "AccountLocked",
    "AccountRestricted",
    "EmailAlreadyVerified",
    "EmailVerificationRequired",
    "InvalidVerificationCode",
    "VerificationAttemptsExceeded",
    "VerificationCodeExpired",
    "VerificationResendTooSoon",
    "AuthenticationRequired",
    "ExpiredRefreshToken",
    "ExpiredToken",
    "InactiveAccount",
    "InvalidCredentials",
    "InvalidRefreshToken",
    "InvalidResetToken",
    "InvalidSignature",
    "InvalidToken",
    "InvalidVerificationToken",
    "MissingToken",
    "RevokedSession",
    "SessionNotFound",
    "WeakPassword",
]
