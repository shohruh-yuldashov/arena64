"""app.common.middleware — request-id and correlation-id, independently.

Uses throwaway apps rather than the real one, so each middleware is
exercised in isolation, proving neither depends on the other running
(common/middleware.py's whole reason for existing as two classes).
"""

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.common.middleware import CorrelationIdMiddleware, RequestIdMiddleware

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _app_with(*middlewares: type[BaseHTTPMiddleware]) -> FastAPI:
    app = FastAPI()
    for middleware in middlewares:
        app.add_middleware(middleware)

    @app.get("/probe")
    async def probe() -> dict[str, str]:
        return {}

    return app


class TestRequestIdMiddleware:
    def test_mints_a_fresh_id_every_request(self) -> None:
        with TestClient(_app_with(RequestIdMiddleware)) as client:
            first = client.get("/probe").headers["X-Request-Id"]
            second = client.get("/probe").headers["X-Request-Id"]

        assert _UUID_RE.match(first)
        assert first != second

    def test_ignores_a_client_supplied_request_id(self) -> None:
        # services.md §8.2: generated at the edge, never accepted from the
        # caller — a client-supplied id could collide with or spoof another
        # client's in the logs.
        with TestClient(_app_with(RequestIdMiddleware)) as client:
            response = client.get("/probe", headers={"X-Request-Id": "client-supplied"})

        assert response.headers["X-Request-Id"] != "client-supplied"


class TestCorrelationIdMiddleware:
    def test_mints_a_fresh_id_when_none_is_supplied(self) -> None:
        with TestClient(_app_with(CorrelationIdMiddleware)) as client:
            response = client.get("/probe")

        assert _UUID_RE.match(response.headers["X-Correlation-Id"])

    def test_propagates_a_caller_supplied_correlation_id(self) -> None:
        with TestClient(_app_with(CorrelationIdMiddleware)) as client:
            response = client.get("/probe", headers={"X-Correlation-Id": "upstream-chain-id"})

        assert response.headers["X-Correlation-Id"] == "upstream-chain-id"


class TestMiddlewaresAreIndependent:
    def test_correlation_id_does_not_default_to_the_request_id(self) -> None:
        # The A64-006 behaviour this refactor deliberately removed: the two
        # must be free to differ, since a correlation id is caller-supplied
        # and a request id never is.
        with TestClient(_app_with(RequestIdMiddleware, CorrelationIdMiddleware)) as client:
            response = client.get("/probe")

        assert response.headers["X-Request-Id"] != response.headers["X-Correlation-Id"]

    def test_either_middleware_works_with_the_other_absent(self) -> None:
        with TestClient(_app_with(RequestIdMiddleware)) as client:
            response = client.get("/probe")
        assert "X-Request-Id" in response.headers
        assert "X-Correlation-Id" not in response.headers
