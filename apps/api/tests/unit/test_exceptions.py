"""Exception taxonomy -> HTTP status mapping — services.md §7.2.

Uses a throwaway app rather than the real one, so this suite exercises
`register_exception_handlers` in isolation from routing, settings, and
lifespan concerns.
"""

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.exception_handlers import register_exception_handlers
from app.core.exceptions import (
    Arena64Error,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
    RateLimitedError,
    RuleViolationError,
    TransientInfrastructureError,
    ValidationError,
)


def _app_raising(exc: Exception) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    return app


@pytest.mark.parametrize(
    ("make_exc", "expected_status", "expected_code"),
    [
        (lambda: ValidationError("bad input"), 422, "validation_error"),
        (lambda: NotFoundError("missing"), 404, "not_found"),
        (lambda: ConflictError("already exists"), 409, "conflict"),
        (lambda: PermissionDeniedError("no"), 403, "permission_denied"),
        (lambda: PreconditionFailedError("stale version"), 412, "precondition_failed"),
        (lambda: RuleViolationError("illegal move"), 422, "rule_violation"),
        (lambda: RateLimitedError("slow down"), 429, "rate_limited"),
        (lambda: TransientInfrastructureError("db hiccup"), 503, "transient_infrastructure_error"),
    ],
)
def test_arena64_error_maps_to_its_documented_status(
    make_exc: Callable[[], Arena64Error], expected_status: int, expected_code: str
) -> None:
    exc = make_exc()
    with TestClient(_app_raising(exc), raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == expected_status
    body = response.json()
    assert body["code"] == expected_code
    assert body["message"] == exc.message
    assert "request_id" in body
    assert "correlation_id" in body


def test_unhandled_exception_returns_500_without_leaking_its_message() -> None:
    with TestClient(
        _app_raising(RuntimeError("leaked internal detail")), raise_server_exceptions=False
    ) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert "leaked internal detail" not in body["message"]


def test_domain_error_subtype_not_explicitly_mapped_falls_back_through_its_parent() -> None:
    class SomeFutureDomainRule(ConflictError):
        pass

    exc = SomeFutureDomainRule("a rule not yet enumerated in the mapping table")
    with TestClient(_app_raising(exc), raise_server_exceptions=False) as client:
        response = client.get("/boom")

    # Resolves through ConflictError's status via MRO walk, not the 500 fallback.
    assert response.status_code == 409
