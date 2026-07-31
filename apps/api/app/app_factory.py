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
from app.common.middleware import CorrelationIdMiddleware
from app.config.settings import get_settings
from app.core.constants import API_PREFIX
from app.database.engine import create_engine, create_session_factory
from app.database.redis import create_redis_pools

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

    engine = create_engine(settings.postgres)
    session_factory = create_session_factory(engine)
    redis_pools = create_redis_pools(settings.redis)

    # Everything a route reaches via app/api/deps.py lives here. This is
    # the HTTP-native half of the bridge described in deps.py's docstring;
    # a future non-HTTP entrypoint resolves the same singletons through
    # app.core.di.Container instead, since it has no app.state.
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis_pools = redis_pools

    logger.info("startup_complete")
    try:
        yield
    finally:
        logger.info("shutdown_begin")
        await redis_pools.aclose()
        await engine.dispose()
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

    app.add_middleware(CorrelationIdMiddleware)
    register_exception_handlers(app)

    # Unversioned, for load-balancer and orchestrator probes: a liveness
    # check must not sit behind API versioning that could itself fail to
    # resolve (app/api/v1/health.py's docstring).
    app.include_router(health_router)
    app.include_router(api_router, prefix=API_PREFIX)

    return app
