"""The app must start and answer health checks, and readiness must degrade
gracefully rather than raise when a dependency is unreachable (CLAUDE.md §9
rule 8: distinguish expected from exceptional — a down dependency is an
expected outcome for a readiness probe, not a defect)."""

import pytest
from fastapi.testclient import TestClient


def test_liveness_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_liveness_is_also_mounted_under_the_versioned_api(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_has_the_documented_shape(client: TestClient) -> None:
    # Deliberately does not assert specific true/false values here: whether
    # a developer happens to have Postgres or Redis running locally is not
    # something this test should depend on (CLAUDE.md testing rule 4 —
    # deterministic, no dependence on the surrounding environment). The
    # unreachable-dependency path is exercised deterministically below.
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["postgres"], bool)
    assert set(body["redis"]) == {"live", "bus", "broker", "cache"}


def test_readiness_reports_degraded_when_dependencies_are_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `invalid.invalid` is reserved by RFC 2606 to never resolve, so this
    # fails deterministically regardless of what is or is not running on
    # the host — unlike pointing at "localhost", which may or may not have
    # something listening depending on the machine.
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+asyncpg://u:p@invalid.invalid:5432/db")
    monkeypatch.setenv("REDIS_LIVE_URL", "redis://invalid.invalid:6379/0")
    monkeypatch.setenv("REDIS_BUS_URL", "redis://invalid.invalid:6379/1")
    monkeypatch.setenv("REDIS_BROKER_URL", "redis://invalid.invalid:6379/2")
    monkeypatch.setenv("REDIS_CACHE_URL", "redis://invalid.invalid:6379/3")

    from app.app_factory import create_app

    with TestClient(create_app()) as unreachable_client:
        response = unreachable_client.get("/api/v1/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["postgres"] is False
    assert body["redis"] == {"live": False, "bus": False, "broker": False, "cache": False}


def test_response_carries_correlation_headers(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["X-Request-Id"]
    assert response.headers["X-Correlation-Id"]


def test_unknown_route_returns_404(client: TestClient) -> None:
    # Routing-level 404 — handled by Starlette before any Arena64Error can
    # be raised, so this does not go through app/api/exception_handlers.py.
    # See test_exceptions.py for the taxonomy's own mapping.
    response = client.get("/this-route-does-not-exist")
    assert response.status_code == 404
