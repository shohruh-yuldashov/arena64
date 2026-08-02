"""The error taxonomy — services.md §7.1.

Rooted here so every layer can raise and catch it without importing a
framework. services.md places this in `shared/` because a domain kernel
must be importable from `domain/`; the structure required for this bootstrap
has no `shared/` package and no `domain/` layers yet (no modules exist), so
it lives in `core/` for now — see this task's closing notes on reconciling
the two structures once the first module is built.

    Arena64Error
    +-- ValidationError            the input was malformed
    +-- DomainError                a rule said no — a normal outcome (BE-07)
    |   +-- AuthenticationFailed    401 — identity not proven
    |   +-- NotFoundError
    |   +-- ConflictError
    |   |   +-- TemporaryConflictError  409 + a retry hint (A64-015.5)
    |   +-- PermissionDeniedError
    |   +-- PreconditionFailedError
    |   +-- RuleViolationError
    |   +-- RateLimitedError
    |       +-- TooManyRequests    429, carrying retry metadata (A64-011.8)
    +-- InfrastructureError        a dependency failed
        +-- TransientInfrastructureError   retryable
        +-- PermanentInfrastructureError   not retryable

Every member's `default_code` is an `ErrorCode` (app.core.error_codes) —
strongly typed rather than a bare string, so a typo in a future subclass
(`"nto_found"`) fails at the type checker instead of shipping as a silent
new, undocumented wire code.
"""

from typing import ClassVar

from app.core.error_codes import ErrorCode


class Arena64Error(Exception):
    """Root of the taxonomy. Carries a safe, client-facing `message` and a
    stable `code` — never a stack trace, SQL, or an internal identifier
    (services.md §7.2 rule 4)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code: ErrorCode = code or self.default_code


class ValidationError(Arena64Error):
    """The caller sent something malformed. Not retryable. Logged at DEBUG."""

    default_code: ClassVar[ErrorCode] = ErrorCode.VALIDATION_ERROR


class DomainError(Arena64Error):
    """A rule said no. **Not an application error** (BE-07) — this is a
    normal outcome of business logic, never logged above INFO and never
    paged on. If domain errors were logged as errors, the noise floor from
    ordinary rejections would bury the signal that actually matters
    (services.md §7.1)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.DOMAIN_ERROR


class AuthenticationFailed(DomainError):
    """The caller did not prove who they are — HTTP 401.

    Distinct from `PermissionDeniedError` (403), and the distinction is not
    pedantry: 401 means "I do not know who you are, try authenticating",
    403 means "I know who you are and you may not do this". A client
    behaves differently on each — 401 sends you to a sign-in form, 403
    does not, and sending an already-signed-in user back to sign in
    because the platform conflated the two is a real and common bug.

    Added in A64-011.2 because login is the first thing on the platform
    that can fail for want of identity. Everything before it either
    succeeded or failed for a reason 403/404/409 already described.

    Note for A64-011.3: RFC 9110 §11.6.1 says a 401 response *must* carry
    a `WWW-Authenticate` header naming the scheme. This platform has no
    scheme yet — bearer tokens arrive with JWT — so the header is
    deliberately omitted rather than asserting a scheme that does not
    exist. Adding `WWW-Authenticate: Bearer` belongs with the tokens.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.AUTHENTICATION_FAILED


class NotFoundError(DomainError):
    default_code: ClassVar[ErrorCode] = ErrorCode.NOT_FOUND


class ConflictError(DomainError):
    default_code: ClassVar[ErrorCode] = ErrorCode.CONFLICT


class TemporaryConflictError(ConflictError):
    """A conflict that resolves on its own after a stated interval —
    A64-015.5.

    Still a `409`: the platform's *state* refused the request, and the
    caller did nothing wrong. What it adds is a **retry hint**, which is the
    one thing that distinguishes "you are already queued" (fix it yourself,
    or wait for the match you have) from "you declined a match a moment ago"
    (do nothing, and try again in forty seconds).

    ## Why this is in the core taxonomy rather than in the module

    So that transport can render it without knowing which module raised it.
    `app/api/exception_handlers.py` emits `Retry-After` for anything that is
    one of these, exactly as it does for `TooManyRequests`, and it does that
    by naming a type in `app.core` — never by importing
    `matchmaking.domain.exceptions`, which would couple the platform's error
    rendering to one bounded context and make every future module with a
    cooldown edit this file.

    AD-09's gateway renders the same exception as an error frame carrying
    the same number and no header, which is the reason the interval lives on
    the *exception* as a float rather than as a pre-formatted header string.

    ## `retry_after_seconds` is derived from stored state, never from config

    A caller that reported the configured window would be wrong for anything
    that extends — see `QueueCooldownActive`, where a second decline
    lengthens the bar and the honest answer is what the row now says.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float,
        code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.retry_after_seconds = retry_after_seconds


class PermissionDeniedError(DomainError):
    default_code: ClassVar[ErrorCode] = ErrorCode.PERMISSION_DENIED


class PreconditionFailedError(DomainError):
    default_code: ClassVar[ErrorCode] = ErrorCode.PRECONDITION_FAILED


class RuleViolationError(DomainError):
    """e.g. an illegal move, once `game` exists to raise one."""

    default_code: ClassVar[ErrorCode] = ErrorCode.RULE_VIOLATION


class RateLimitedError(DomainError):
    default_code: ClassVar[ErrorCode] = ErrorCode.RATE_LIMITED


class TooManyRequests(RateLimitedError):
    """A rate limit refused the request — HTTP 429 (A64-011.8).

    The concrete member of `RateLimitedError`, which existed from A64-006
    as a taxonomy placeholder with nothing able to raise it. This is what
    raises.

    ## Why it carries numbers and not headers

    It exposes `retry_after`, `limit`, `remaining` and `reset_after` as
    plain values, and deliberately does **not** know that they are rendered
    as `Retry-After` and `X-RateLimit-*`. Transport meaning does not live
    in `core/` — services.md is explicit that "the same
    `PreconditionFailed` maps to an HTTP status in `entrypoints/http` and
    to a WebSocket error frame code in `entrypoints/gateway`", and this
    exception has both futures: AD-09's gateway rate limits per connection
    (architecture.md §10) and will refuse a frame with an error *frame*,
    which has no headers at all.

    `app/api/exception_handlers.py` owns the HTTP rendering.

    ## Why `message` says nothing specific

    "Too many requests. Try again later." — the same string whatever rule
    fired, whatever endpoint it fired on, and whichever of an IP or an
    email bucket was exhausted.

    Naming the rule would tell an attacker which dimension they tripped,
    which is precisely the information needed to evade it: "per email" says
    rotate the address, "per IP" says rotate the host. The numbers in the
    headers are a deliberate exception to that reticence — a legitimate
    client genuinely needs to know when to retry, and the numbers describe
    the *binding* rule without naming which one it is.
    """

    def __init__(
        self,
        message: str = "Too many requests. Try again later.",
        *,
        retry_after: int,
        limit: int,
        remaining: int = 0,
        reset_after: int,
        code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.retry_after = retry_after
        """Whole seconds until the caller may retry. Never zero — see
        `RateLimitDecision.retry_after_seconds`."""

        self.limit = limit
        self.remaining = remaining
        self.reset_after = reset_after


class InfrastructureError(Arena64Error):
    """A dependency failed. Adapters translate driver exceptions into one of
    the two subclasses below at the infrastructure boundary — a raw driver
    exception must never escape into `application/` (services.md §7.2)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.INFRASTRUCTURE_ERROR


class TransientInfrastructureError(InfrastructureError):
    """Deadlock, connection reset, timeout, lock contention. Retryable with
    bounded backoff (services.md §7.3)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.TRANSIENT_INFRASTRUCTURE_ERROR


class PermanentInfrastructureError(InfrastructureError):
    """Misconfiguration, missing relation, auth failure to a dependency. Not
    retryable — a human must look."""

    default_code: ClassVar[ErrorCode] = ErrorCode.PERMANENT_INFRASTRUCTURE_ERROR
