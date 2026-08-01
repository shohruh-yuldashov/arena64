"""The application factory — the composition root for the HTTP runtime
profile (architecture.md AD-02).

Uses `lifespan`, not the deprecated `@app.on_event("startup"/"shutdown")`
hooks: everything a request handler reaches through `app.state` or
`Depends` is built in one place, in a defined order, with a matching
teardown — not scattered across decorators that run in registration order
with no enforced pairing.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.exception_handlers import register_exception_handlers
from app.api.router import api_router
from app.api.v1.health import health_router
from app.common.logging import configure_logging
from app.common.middleware import CorrelationIdMiddleware, RequestIdMiddleware
from app.config.settings import get_settings
from app.core.clock import SystemClock
from app.core.constants import API_PREFIX
from app.database.rate_limiter import RedisRateLimiter
from app.database.redis import create_redis_pools
from app.database.session_manager import DatabaseSessionManager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown, in one place and in a defined order."""
    # DI-06: settings are validated here, before the process accepts a
    # single request. A missing or malformed value raises out of
    # get_settings() and the process never reaches `yield` — a visible,
    # automatically-rolled-back deploy failure, not a 3am outage.
    settings = get_settings()

    configure_logging(
        level=settings.app.log_level,
        environment=settings.environment,
        format_override=settings.app.log_format,
    )
    logger.info("startup_begin", extra={"environment": settings.environment.value})

    db = DatabaseSessionManager(settings.postgres)
    redis_pools = create_redis_pools(settings.redis)

    # A64-011.8. Built once per process rather than per request: the
    # constructor computes the SHA of its Lua script so every check can use
    # EVALSHA, and that work is wasted if it is redone on the hottest
    # unauthenticated path on the platform. See `app/api/deps.py`.
    #
    # Uses the `limits` Redis role, never `cache` — a rate limit counter
    # evicted under memory pressure is a limit that disappears during
    # exactly the traffic spike it exists for (`RedisSettings`).
    rate_limiter = RedisRateLimiter(
        redis_pools.limits,
        settings=settings.rate_limit,
        clock=SystemClock(),
    )

    if not settings.rate_limit.enabled:
        # Loud, and at WARNING, because the kill switch is invisible
        # otherwise: an endpoint with no limiter looks exactly like an
        # endpoint with a working one until somebody attacks it.
        logger.warning(
            "rate_limiting_disabled",
            extra={"environment": settings.environment.value},
        )

    # Everything a route reaches via app/api/deps.py lives here. This is
    # the HTTP-native half of the bridge described in deps.py's docstring;
    # a future non-HTTP entrypoint resolves the same singletons through
    # app.core.di.Container instead, since it has no app.state.
    app.state.settings = settings
    app.state.db = db
    app.state.redis_pools = redis_pools
    app.state.rate_limiter = rate_limiter

    logger.info("startup_complete")
    try:
        yield
    finally:
        logger.info("shutdown_begin")
        await redis_pools.aclose()
        await db.close()
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    """Assembles middleware, exception handlers, and routers. Wires nothing
    a route handler could not otherwise reach through `app.state` or
    `Depends` — see dependency-injection.md DI-01.
    """
    app = FastAPI(
        title="Arena64 API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Order is immaterial: A64-008 decoupled the two middlewares (see
    # common/middleware.py's docstring), so neither depends on the other
    # having already run.
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)

    # Unversioned, for load-balancer and orchestrator probes: a liveness
    # check must not sit behind API versioning that could itself fail to
    # resolve (app/api/v1/health.py's docstring).
    app.include_router(health_router)
    app.include_router(api_router, prefix=API_PREFIX)

    return app
