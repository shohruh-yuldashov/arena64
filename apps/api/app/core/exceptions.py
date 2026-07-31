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
    |   +-- NotFoundError
    |   +-- ConflictError
    |   +-- PermissionDeniedError
    |   +-- PreconditionFailedError
    |   +-- RuleViolationError
    |   +-- RateLimitedError
    +-- InfrastructureError        a dependency failed
        +-- TransientInfrastructureError   retryable
        +-- PermanentInfrastructureError   not retryable
"""

from typing import ClassVar


class Arena64Error(Exception):
    """Root of the taxonomy. Carries a safe, client-facing `message` and a
    stable `code` — never a stack trace, SQL, or an internal identifier
    (services.md §7.2 rule 4)."""

    default_code: ClassVar[str] = "internal_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code


class ValidationError(Arena64Error):
    """The caller sent something malformed. Not retryable. Logged at DEBUG."""

    default_code: ClassVar[str] = "validation_error"


class DomainError(Arena64Error):
    """A rule said no. **Not an application error** (BE-07) — this is a
    normal outcome of business logic, never logged above INFO and never
    paged on. If domain errors were logged as errors, the noise floor from
    ordinary rejections would bury the signal that actually matters
    (services.md §7.1)."""

    default_code: ClassVar[str] = "domain_error"


class NotFoundError(DomainError):
    default_code: ClassVar[str] = "not_found"


class ConflictError(DomainError):
    default_code: ClassVar[str] = "conflict"


class PermissionDeniedError(DomainError):
    default_code: ClassVar[str] = "permission_denied"


class PreconditionFailedError(DomainError):
    default_code: ClassVar[str] = "precondition_failed"


class RuleViolationError(DomainError):
    """e.g. an illegal move, once `game` exists to raise one."""

    default_code: ClassVar[str] = "rule_violation"


class RateLimitedError(DomainError):
    default_code: ClassVar[str] = "rate_limited"


class InfrastructureError(Arena64Error):
    """A dependency failed. Adapters translate driver exceptions into one of
    the two subclasses below at the infrastructure boundary — a raw driver
    exception must never escape into `application/` (services.md §7.2)."""

    default_code: ClassVar[str] = "infrastructure_error"


class TransientInfrastructureError(InfrastructureError):
    """Deadlock, connection reset, timeout, lock contention. Retryable with
    bounded backoff (services.md §7.3)."""

    default_code: ClassVar[str] = "transient_infrastructure_error"


class PermanentInfrastructureError(InfrastructureError):
    """Misconfiguration, missing relation, auth failure to a dependency. Not
    retryable — a human must look."""

    default_code: ClassVar[str] = "permanent_infrastructure_error"
