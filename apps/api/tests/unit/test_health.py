"""The app must start and answer health checks, and readiness must answer
**in its status line** when a dependency is unreachable.

A64-028.6 §9 changed that last part and this file records why. Until then
readiness returned HTTP 200 with `status: "degraded"` even with PostgreSQL
and Redis both unreachable, and these tests asserted it — a load balancer
reads the status line and nothing in a fleet parses the body, so a
database-less instance stayed in rotation and kept failing requests. That
was A64-028.1's P1-5.

Degrading gracefully rather than raising is still the rule (CLAUDE.md §9
rule 8: a down dependency is an expected outcome for a probe, not a
defect). What changed is that the expected outcome is now **503** and the
diagnostic body is unchanged beside it.

Every success body is asserted against the standard `{data, meta}` envelope
(app.core.responses.ApiResponse) — health is the first real consumer that
proves the wrapper works end to end.
"""

import pytest
from fastapi.testclient import TestClient


def test_liveness_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["data"] == {"status": "ok"}
    assert "request_id" in body["meta"]
    assert "correlation_id" in body["meta"]


def test_liveness_is_also_mounted_under_the_versioned_api(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["data"] == {"status": "ok"}


def test_readiness_has_the_documented_shape(client: TestClient) -> None:
    # Deliberately does not assert specific true/false values here: whether
    # a developer happens to have Postgres or Redis running locally is not
    # something this test should depend on (CLAUDE.md testing rule 4 —
    # deterministic, no dependence on the surrounding environment). The
    # unreachable-dependency path is exercised deterministically below.
    response = client.get("/api/v1/health/ready")
    assert response.status_code in {200, 503}
    data = response.json()["data"]
    assert data["status"] in {"ok", "degraded"}
    assert isinstance(data["postgres"], bool)
    assert set(data["redis"]) == {"live", "bus", "broker", "cache", "limits"}


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
    monkeypatch.setenv("REDIS_LIMITS_URL", "redis://invalid.invalid:6379/4")

    from app.app_factory import create_app

    with TestClient(create_app()) as unreachable_client:
        response = unreachable_client.get("/api/v1/health/ready")

    # The status line, which is the whole of P1-5: 200 here meant a
    # balancer kept routing to an instance with no database.
    assert response.status_code == 503
    data = response.json()["data"]
    assert data["status"] == "degraded"
    assert data["postgres"] is False
    assert data["redis"] == {
        "live": False,
        "bus": False,
        "broker": False,
        "cache": False,
        "limits": False,
    }


def test_response_carries_correlation_headers(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["X-Request-Id"]
    assert response.headers["X-Correlation-Id"]


def test_response_headers_match_the_envelope_meta(client: TestClient) -> None:
    response = client.get("/health")
    meta = response.json()["meta"]
    assert response.headers["X-Request-Id"] == meta["request_id"]
    assert response.headers["X-Correlation-Id"] == meta["correlation_id"]


def test_unknown_route_returns_404(client: TestClient) -> None:
    # Routing-level 404 — handled by Starlette before any Arena64Error can
    # be raised, so this does not go through app/api/exception_handlers.py.
    # See test_exceptions.py for the taxonomy's own mapping.
    response = client.get("/this-route-does-not-exist")
    assert response.status_code == 404


def test_readiness_fails_while_the_instance_is_draining(client: TestClient) -> None:
    """A64-028.6 §9 and §11 — the deploy's side of readiness.

    Every dependency is healthy; the instance has simply been told it is
    going away. Readiness must say so, because saying so *before* the
    process is signalled is the only thing that lets a balancer stop
    routing to it in time — which is what P1-6 needed and did not have.
    """
    from app.api.deps import service_lifecycle

    lifecycle = service_lifecycle()
    try:
        lifecycle.begin_drain()
        response = client.get("/api/v1/health/ready")
    finally:
        service_lifecycle.cache_clear()

    assert response.status_code == 503
    data = response.json()["data"]
    assert data["draining"] is True
    # Draining is not a dependency failure, and the body must not blur them:
    # an operator reading this needs to know the deploy did it.
    assert data["postgres"] is True


def test_liveness_stays_ok_while_draining(client: TestClient) -> None:
    """Liveness must not follow readiness down.

    An orchestrator that reads liveness restarts what fails it. A draining
    instance is working perfectly and is about to be replaced on purpose;
    restarting it mid-drain would be the orchestrator undoing the deploy.
    """
    from app.api.deps import service_lifecycle

    try:
        service_lifecycle().begin_drain()
        response = client.get("/health")
    finally:
        service_lifecycle.cache_clear()

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "ok"}
