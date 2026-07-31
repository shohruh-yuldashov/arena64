"""Shared test fixtures.

`ENVIRONMENT=test` is set before anything under `app` is imported, so
`get_settings()` never resolves against `local`'s defaults during
collection (dependency-injection.md §2.3).
"""

import os

os.environ.setdefault("ENVIRONMENT", "test")

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """`get_settings()` is process-cached by design (dependency-injection.md
    DI-06). Tests that vary configuration must not see a stale cached
    `Settings` left over from an earlier test — CLAUDE.md testing rule 7,
    isolation."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient over the real application factory.

    `with TestClient(app) as c:` runs the app's lifespan on enter and exit
    (CLAUDE.md: lifespan, not the deprecated startup/shutdown events) —
    engine and Redis pools are genuinely constructed, exactly as they are
    in production. No connection is attempted until a handler executes a
    query, so this works correctly with no real Postgres or Redis running;
    see tests/unit/test_health.py for the readiness path this makes
    possible to test honestly.
    """
    from app.app_factory import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
