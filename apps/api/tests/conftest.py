"""Shared test fixtures.

`ENVIRONMENT=test` is set before anything under `app` is imported, so
`get_settings()` never resolves against `local`'s defaults during
collection (dependency-injection.md §2.3).
"""

import os

os.environ.setdefault("ENVIRONMENT", "test")

# A64-011.8: rate limiting is **off by default across the suite**, and on
# only in the files that test it.
#
# Not a convenience. Every limit is a shared counter with a window measured
# in minutes or hours, so leaving it on would couple otherwise independent
# tests through Redis: `test_auth_api.py` registers a fresh account per
# test from one apparent IP, and the fourth of those would be refused by a
# limit of three per hour. The failure would be order-dependent, would only
# appear once the file grew past the limit, and would be blamed on
# registration rather than on the limiter.
#
# The alternative — flushing Redis between tests — makes every suite on the
# platform depend on a reachable Redis to test things that have nothing to
# do with it.
#
# What keeps this from hiding a regression is that the rate-limiting suites
# turn it back on explicitly, and `test_auth_rate_limits.py` asserts
# *structurally* that each of the six endpoints still carries its guard —
# an assertion this switch cannot affect.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

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
