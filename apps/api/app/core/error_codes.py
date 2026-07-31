"""The error code registry — services.md §7.2 rule 4: every error carries
"a stable, machine-readable code" on the wire. Before this file, that code
was a bare string constant scattered across `exceptions.py` (`default_code:
ClassVar[str] = "not_found"`) — correct on the wire, but with no single
place enumerating every code that exists, which is what a frontend needs to
build an exhaustive `switch` over, and what an OpenAPI schema needs to
render as an enum instead of an unconstrained string.

One registry, `exceptions.py` references it, `exception_handlers.py`
serialises it, and `apps/web/src/types/api.ts` mirrors it by hand — the
one seam that can't be shared across a Python/TypeScript boundary without
a codegen step this platform doesn't have yet (a documented follow-up, not
a gap introduced silently).
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Every code an `Arena64Error` can carry. Additive only — see
    `Arena64Error`'s own note on why: removing or renaming a member breaks
    every client that branches on it, including ones already deployed.

    **When a module earns its own code, and when it doesn't.** A module's
    exception class always exists (`UserNotFound`, `IllegalMove`) — that is
    what server-side code branches on. A *new wire code* is added here only
    when a client must be able to behave differently for it, and the HTTP
    status plus the endpoint it came from are not enough to tell it apart.
    `UserNotFound` on `GET /users/{id}` needs no code of its own — `404` +
    `not_found` already says everything a client can act on. But a `409`
    from a registration form must say *which* field collided, so
    `USERNAME_ALREADY_EXISTS` and `EMAIL_ALREADY_EXISTS` are distinct
    codes. Without that rule this enum grows a member per exception class
    per module and stops being a useful thing to exhaustively switch over.
    """

    # Arena64Error itself — the fallback when nothing more specific applies.
    INTERNAL_ERROR = "internal_error"

    # ValidationError
    VALIDATION_ERROR = "validation_error"

    # DomainError and its children — services.md BE-07: normal outcomes,
    # never logged as errors.
    DOMAIN_ERROR = "domain_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PERMISSION_DENIED = "permission_denied"
    PRECONDITION_FAILED = "precondition_failed"
    RULE_VIOLATION = "rule_violation"
    RATE_LIMITED = "rate_limited"

    # InfrastructureError and its children
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    TRANSIENT_INFRASTRUCTURE_ERROR = "transient_infrastructure_error"
    PERMANENT_INFRASTRUCTURE_ERROR = "permanent_infrastructure_error"

    # --- module-specific codes, per the rule in this class's docstring ---
    # `users` (A64-010): a sign-up or profile form must know which field
    # collided to highlight it; a bare `conflict` cannot say.
    USERNAME_ALREADY_EXISTS = "username_already_exists"
    EMAIL_ALREADY_EXISTS = "email_already_exists"
