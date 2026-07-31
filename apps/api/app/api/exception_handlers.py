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
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.common.context import current_request_id
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
    stack trace, SQL, or an internal identifier."""

    code: str
    message: str
    request_id: str | None = None


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
    body = ErrorResponse(code=exc.code, message=exc.message, request_id=current_request_id())
    return JSONResponse(status_code=_status_for(exc), content=body.model_dump())


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """A defect, not a domain outcome — services.md §7.1: "Unhandled
    exception ... ERROR with stack". The client never sees `str(exc)`, only
    a stable, generic message (services.md §7.2 rule 4: never leak stack
    traces, SQL, or internal identifiers to a client)."""
    logger.error("unhandled_exception", exc_info=exc)
    body = ErrorResponse(
        code="internal_error",
        message="An unexpected error occurred.",
        request_id=current_request_id(),
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body.model_dump()
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(Arena64Error, _handle_arena64_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
