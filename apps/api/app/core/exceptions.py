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
    |   +-- PermissionDeniedError
    |   +-- PreconditionFailedError
    |   +-- RuleViolationError
    |   +-- RateLimitedError
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


class PermissionDeniedError(DomainError):
    default_code: ClassVar[ErrorCode] = ErrorCode.PERMISSION_DENIED


class PreconditionFailedError(DomainError):
    default_code: ClassVar[ErrorCode] = ErrorCode.PRECONDITION_FAILED


class RuleViolationError(DomainError):
    """e.g. an illegal move, once `game` exists to raise one."""

    default_code: ClassVar[ErrorCode] = ErrorCode.RULE_VIOLATION


class RateLimitedError(DomainError):
    default_code: ClassVar[ErrorCode] = ErrorCode.RATE_LIMITED


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
