"""HTTP exception handling — services.md §7.2.

"One transport mapping table per entrypoint, in the interface layer... the
same `PreconditionFailed` maps to an HTTP status in `entrypoints/http` and
to a WebSocket error frame code in `entrypoints/gateway` — services serve
four callers; transport meaning cannot live in them." This is that table
for HTTP, and only for HTTP; it imports FastAPI on purpose, which is why it
lives under `app/api/` and not `app/core/` (dependency-injection.md §3.2:
`core/` must never contain FastAPI).
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.common.context import current_correlation_id, current_request_id
from app.core.error_codes import ErrorCode
from app.core.exceptions import (
    Arena64Error,
    ConflictError,
    DomainError,
    NotFoundError,
    PermanentInfrastructureError,
    PermissionDeniedError,
    PreconditionFailedError,
    RateLimitedError,
    RuleViolationError,
    TransientInfrastructureError,
    ValidationError,
)

logger = logging.getLogger(__name__)


class ErrorResponse(BaseModel):
    """The only shape an Arena64 error takes on the wire: a safe message and
    a stable, machine-readable code (services.md §7.2 rule 4) — never a
    stack trace, SQL, or an internal identifier.

    Deliberately not `app.core.responses.ApiResponse` — nesting an error
    under `data` would make a client check "did this succeed" by inspecting
    the body instead of the HTTP status, the one signal that is never
    ambiguous. `request_id` and `correlation_id` mirror `ResponseMeta`'s
    fields directly rather than reusing it, so an error body never implies
    it carries a `data` field it doesn't have.
    """

    code: ErrorCode
    message: str
    request_id: str | None = None
    correlation_id: str | None = None


# Ordered by specificity; `_status_for` walks the MRO, so a future subtype
# not listed here still resolves through its nearest listed ancestor
# instead of falling straight through to 500.
_STATUS_BY_EXCEPTION: dict[type[Arena64Error], int] = {
    ValidationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    PermissionDeniedError: status.HTTP_403_FORBIDDEN,
    PreconditionFailedError: status.HTTP_412_PRECONDITION_FAILED,
    RuleViolationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RateLimitedError: status.HTTP_429_TOO_MANY_REQUESTS,
    DomainError: status.HTTP_400_BAD_REQUEST,
    TransientInfrastructureError: status.HTTP_503_SERVICE_UNAVAILABLE,
    PermanentInfrastructureError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    Arena64Error: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _status_for(exc: Arena64Error) -> int:
    for klass in type(exc).__mro__:
        if klass in _STATUS_BY_EXCEPTION:
            return _STATUS_BY_EXCEPTION[klass]
    # pragma: no cover — Arena64Error itself is always in the map above
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _log(exc: Arena64Error) -> None:
    """services.md §7.1's level table. A `DomainError` is a normal outcome
    (BE-07) and is never logged as an error; an infrastructure failure is.
    """
    if isinstance(exc, ValidationError):
        logger.debug("validation_error", extra={"code": exc.code})
    elif isinstance(exc, DomainError):
        logger.info("domain_error", extra={"code": exc.code})
    elif isinstance(exc, TransientInfrastructureError):
        logger.warning("transient_infrastructure_error", extra={"code": exc.code})
    else:
        logger.error("infrastructure_error", extra={"code": exc.code}, exc_info=exc)


async def _handle_arena64_error(request: Request, exc: Exception) -> JSONResponse:
    # pragma: no cover — FastAPI only ever routes an Arena64Error to this handler
    if not isinstance(exc, Arena64Error):
        raise TypeError(f"handler registered for Arena64Error, received {type(exc)!r}")
    _log(exc)
    body = ErrorResponse(
        code=exc.code,
        message=exc.message,
        request_id=current_request_id(),
        correlation_id=current_correlation_id(),
    )
    return JSONResponse(status_code=_status_for(exc), content=body.model_dump(mode="json"))


def _describe_validation_failure(exc: RequestValidationError) -> str:
    """Summarises FastAPI's structured error list into one safe sentence.

    Uses each error's `loc` and `msg` and deliberately drops its `input`
    and `ctx`: `input` echoes back exactly what the client sent, which on
    a registration or profile route is personal data that would then land
    in every error log and browser console (services.md §8.5).
    """
    parts: list[str] = []
    for error in exc.errors():
        # `loc` begins with the source ("body", "query", "path"); joining
        # the whole tuple gives "body.timezone", which is what a client
        # needs to know which field to highlight.
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = error.get("msg", "Invalid value")
        parts.append(f"{location}: {message}" if location else message)

    return "; ".join(parts) or "Request validation failed."


async def _handle_request_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Maps FastAPI's own request-validation failure into the platform
    envelope.

    Without this, FastAPI returns its native `{"detail": [...]}` shape,
    which is the *only* error on the platform that does not carry `code`,
    `message`, `request_id` and `correlation_id` — so
    `apps/web/src/services/error-parser.ts` cannot read it, and a client
    gets "Request failed with status 422" instead of which field was
    wrong. It went unnoticed until A64-010 because no endpoint before it
    accepted a request body or a typed path parameter.

    A malformed request is a `ValidationError` in the platform taxonomy
    (services.md §7.1), so it logs at DEBUG and carries `validation_error`
    — the same code a domain validator raises, because a client cannot
    act differently on "the shape was wrong" versus "the value was wrong".
    """
    # pragma: no cover — registered for this exact type only
    if not isinstance(exc, RequestValidationError):
        raise TypeError(f"handler registered for RequestValidationError, received {type(exc)!r}")

    logger.debug("request_validation_error", extra={"error_count": len(exc.errors())})
    body = ErrorResponse(
        code=ErrorCode.VALIDATION_ERROR,
        message=_describe_validation_failure(exc),
        request_id=current_request_id(),
        correlation_id=current_correlation_id(),
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=body.model_dump(mode="json"),
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """A defect, not a domain outcome — services.md §7.1: "Unhandled
    exception ... ERROR with stack". The client never sees `str(exc)`, only
    a stable, generic message (services.md §7.2 rule 4: never leak stack
    traces, SQL, or internal identifiers to a client)."""
    logger.error("unhandled_exception", exc_info=exc)
    body = ErrorResponse(
        code=ErrorCode.INTERNAL_ERROR,
        message="An unexpected error occurred.",
        request_id=current_request_id(),
        correlation_id=current_correlation_id(),
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=body.model_dump(mode="json"),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(Arena64Error, _handle_arena64_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
