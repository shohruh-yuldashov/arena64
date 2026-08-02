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
from math import ceil

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.rate_limiting import (
    RATE_LIMIT_LIMIT_HEADER,
    RATE_LIMIT_REMAINING_HEADER,
    RATE_LIMIT_RESET_HEADER,
    RETRY_AFTER_HEADER,
)
from app.common.context import current_correlation_id, current_request_id
from app.core.error_codes import ErrorCode
from app.core.exceptions import (
    Arena64Error,
    AuthenticationFailed,
    ConflictError,
    DomainError,
    NotFoundError,
    PermanentInfrastructureError,
    PermissionDeniedError,
    PreconditionFailedError,
    RateLimitedError,
    RuleViolationError,
    TemporaryConflictError,
    TooManyRequests,
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
    AuthenticationFailed: status.HTTP_401_UNAUTHORIZED,
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


#: RFC 6750 §3.1's error codes for the `WWW-Authenticate` challenge. A
#: deliberately smaller vocabulary than the platform's own: the RFC
#: defines exactly `invalid_request`, `invalid_token` and
#: `insufficient_scope`, and inventing others here would produce a
#: challenge that standard HTTP clients cannot parse.
_BEARER_ERROR_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.INVALID_TOKEN: "invalid_token",
    ErrorCode.EXPIRED_TOKEN: "invalid_token",
}


def _challenge_for(exc: Arena64Error) -> str | None:
    """The `WWW-Authenticate` value for a 401, if this is one.

    RFC 9110 §11.6.1 requires a 401 to carry a challenge naming an
    applicable scheme. A64-011.2 deliberately omitted it and said why:
    the platform had no scheme, and asserting `Bearer` before bearer
    tokens existed would have advertised something no endpoint accepted.
    A64-011.3 is the task that made it true, so the header arrives with
    the tokens rather than as an afterthought.

    Two shapes:

    - A bare ``Bearer`` challenge when no token was presented, or when the
      401 came from somewhere other than token verification — a failed
      sign-in at `POST /auth/login` is the current example. There is no
      token to describe, so RFC 6750's `error` parameter does not apply.
    - ``Bearer error="invalid_token", error_description="..."`` when a
      token *was* presented and refused, which is what tells a client
      library to stop retrying with the credential it holds.

    The description is the exception's own message, which is already the
    single generic string for every token failure — deliberately, so this
    header cannot become the oracle the response body refuses to be.
    """
    if not isinstance(exc, AuthenticationFailed):
        return None

    error = _BEARER_ERROR_BY_CODE.get(exc.code)
    if error is None:
        return "Bearer"

    # `error_description` is a quoted-string: RFC 6750 §3 excludes `"` and
    # `\` from it, and every message this platform produces is a fixed
    # literal, but escaping rather than trusting that keeps a future
    # message from silently producing a malformed header.
    description = exc.message.replace("\\", "").replace('"', "")
    return f'Bearer error="{error}", error_description="{description}"'


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


def _headers_for(exc: Arena64Error) -> dict[str, str]:
    """The transport-specific headers an exception's *data* implies.

    This is the mapping table's other half, and the reason
    `TooManyRequests` carries integers rather than header strings: the
    exception knows the caller may retry in 180 seconds, and only this
    layer knows that HTTP spells that `Retry-After: 180`. AD-09's gateway
    will render the same exception as an error frame with no headers at
    all (services.md §7.2: "transport meaning cannot live in [services]").

    `Retry-After` is emitted in delta-seconds rather than as an HTTP-date.
    RFC 9110 §10.2.3 permits both; delta-seconds does not require the
    client's clock to agree with the server's, and a mobile client's often
    does not.
    """
    headers: dict[str, str] = {}

    challenge = _challenge_for(exc)
    if challenge:
        headers["WWW-Authenticate"] = challenge

    if isinstance(exc, TooManyRequests):
        headers[RETRY_AFTER_HEADER] = str(exc.retry_after)
        headers[RATE_LIMIT_LIMIT_HEADER] = str(exc.limit)
        headers[RATE_LIMIT_REMAINING_HEADER] = str(exc.remaining)
        headers[RATE_LIMIT_RESET_HEADER] = str(exc.reset_after)

    # A64-015.5. A decline cooldown is a `409` rather than a `429` — it is
    # the platform's state refusing the request, not a budget the caller
    # spent — and it still carries `Retry-After`, because the header's
    # meaning is "come back after this many seconds" and that is exactly
    # true here. RFC 9110 defines it on any response, not only on 429 and
    # 503, and a client that already backs off on the header gets the right
    # behaviour with no new branch.
    #
    # Rounded **up**: a client that retried at the floor of a fractional
    # second would be refused again for the remainder, which is the one
    # thing a retry hint must not do.
    if isinstance(exc, TemporaryConflictError):
        headers[RETRY_AFTER_HEADER] = str(max(1, ceil(exc.retry_after_seconds)))

    return headers


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
    headers = _headers_for(exc)
    return JSONResponse(
        status_code=_status_for(exc),
        content=body.model_dump(mode="json"),
        headers=headers or None,
    )


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
