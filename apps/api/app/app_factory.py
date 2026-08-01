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
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.exception_handlers import register_exception_handlers
from app.api.router import api_router
from app.api.v1.health import health_router
from app.common.logging import configure_logging
from app.common.middleware import CorrelationIdMiddleware, RequestIdMiddleware
from app.config.settings import Settings, get_settings
from app.core.clock import SystemClock
from app.core.constants import API_PREFIX
from app.database.rate_limiter import RedisRateLimiter
from app.database.redis import create_redis_pools
from app.database.session_manager import DatabaseSessionManager
from app.storage import LocalStorageProvider

logger = logging.getLogger(__name__)

#: Tag metadata for the generated docs — A64-011.9.
#:
#: Without this, `/docs` renders bare tag names above each group and a
#: reader has to infer from the endpoint list what "auth" contains and how
#: it differs from "users". The two are genuinely easy to confuse on this
#: platform, because the split is a *bounded context* boundary rather than
#: an obvious one: `auth` owns proving who you are, `users` owns who you
#: appear to be (architecture.md §6, DM-10). Saying so once, here, is the
#: cheapest place to prevent an endpoint being added to the wrong module.
#:
#: FastAPI renders only tags it finds here *and* on a route, so a tag added
#: to a router without an entry below is not an error — it simply gets no
#: description, exactly as before.
OPENAPI_TAGS: list[dict[str, Any]] = [
    {
        "name": "auth",
        "description": (
            "Proving identity: registration, sign-in, token issue and rotation, "
            "email verification and password recovery.\n\n"
            "Two credentials, and they are not interchangeable. The **access token** "
            "is a short-lived JWT sent as `Authorization: Bearer <token>` and cannot "
            "be revoked before it expires. The **refresh token** is an opaque, "
            "single-use value sent in the request body of `POST /auth/refresh`; it "
            "is rotated on every use, and presenting one that has already been "
            "rotated revokes the entire session chain.\n\n"
            "Endpoints that accept an address or send mail answer identically "
            "whether or not an account exists — deliberately, so that none of them "
            "can be used to discover which addresses are registered."
        ),
    },
    {
        "name": "users",
        "description": (
            "Player identity and profile — who someone *appears to be*, as opposed "
            "to `auth`, which owns proving who they *are*. A player can exist "
            "without an account (a bot seat, a guest), which is why the two are "
            "separate contexts rather than one aggregate."
        ),
    },
    {
        "name": "profiles",
        "description": (
            "Public player profiles, read by handle and by anyone. A profile is a "
            "*composition* rather than a record: identity comes from `users`, ratings "
            "from the rating system and match counts from statistics.\n\n"
            "Ratings and statistics currently report placeholder values — a provisional "
            "starting rating and zero matches — because no game has been played on this "
            "platform yet. The **shape** is final, so a client written against it today "
            "needs no change when real values arrive."
        ),
    },
    {
        "name": "profile",
        "description": (
            "Your own profile — reading it and editing it. Every endpoint acts on the "
            "account behind your access token; there is no path segment or body field "
            "naming an account, so another player's profile cannot be addressed "
            "here.\n\n"
            "Editable: display name, biography, country, interface language and "
            "timezone. **Username and email are not** — changing either has "
            "consequences a profile edit does not (a rename must reserve the old "
            "handle; an email change must re-prove ownership), so each gets its own "
            "flow. Read anyone else's profile at `GET /profiles/{username}`."
        ),
    },
    {
        "name": "avatars",
        "description": (
            "Self-service avatar management for the authenticated account. Every "
            "endpoint acts on **your own** avatar — there is no path segment or body "
            "field naming an account, so another player's avatar cannot be addressed "
            "at all.\n\n"
            "Uploads are validated by file signature rather than by the declared "
            "`Content-Type`, re-encoded to WebP from decoded pixels (which strips EXIF "
            "and every other metadata block), and stored in two sizes. Read anyone's "
            "avatar URL from `GET /profiles/{username}` instead."
        ),
    },
    {
        "name": "health",
        "description": (
            "Liveness and readiness probes for load balancers and orchestrators. "
            "Unversioned and unauthenticated: a probe must not sit behind API "
            "versioning that could itself fail to resolve."
        ),
    },
]


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


def _configure_storage(app: FastAPI, settings: Settings) -> None:
    """Builds the object-storage provider and, in development, serves it.

    **The only place on the platform that names a concrete provider.**
    Everything else — every service, every dependency, every route —
    takes `StorageProvider`. Adding S3, R2, MinIO or GCS is a branch here
    and nothing else, which is what makes A64-012.2's "without changing
    business logic" a structural claim rather than an intention.

    `LocalStorageProvider` refuses to construct in a production-like
    environment (objects would live on one node's disk and vanish on
    reschedule, and a second replica would serve nothing at all), so a
    deployed tier that never sets `STORAGE_PROVIDER` fails at startup
    rather than accepting uploads it is going to lose.

    ## Why this is in `create_app` and not `lifespan`

    Everything else on `app.state` is built in `lifespan`, because engines
    and connection pools need an ordered async teardown. Storage does not:
    the local provider holds a resolved path and nothing else.

    What decided it is the mount. `StaticFiles` has to be part of the
    route table, and an app that skipped `lifespan` — every contract test
    on this platform drives the app over `ASGITransport`, which does — would
    otherwise have a working upload endpoint and no way to fetch what it
    stored. Building both here keeps the app self-contained and makes
    "the URL actually serves" testable, which is exactly the property a
    storage abstraction is easiest to get subtly wrong about.

    A provider that eventually needs an async close — an S3 client — gets
    that wired into `lifespan` at the point it exists, alongside its
    construction.
    """
    storage = LocalStorageProvider(settings.storage, settings.environment)
    app.state.storage = storage

    # Development only, and the guard is the provider's own type rather
    # than an environment check: `LocalStorageProvider` cannot exist in a
    # production-like tier, so neither can this mount.
    #
    # A deployed tier serves objects from the store, or from a CDN in front
    # of it — never through this process. Routing image bytes through
    # Python would put an ASGI worker on the hot path of every avatar
    # render, which is the thing object storage exists to avoid.
    if isinstance(storage, LocalStorageProvider):
        storage.root.mkdir(parents=True, exist_ok=True)
        app.mount(
            settings.storage.public_url_path,
            StaticFiles(directory=storage.root),
            name="media",
        )


def create_app() -> FastAPI:
    """Assembles middleware, exception handlers, and routers. Wires nothing
    a route handler could not otherwise reach through `app.state` or
    `Depends` — see dependency-injection.md DI-01.
    """
    app = FastAPI(
        title="Arena64 API",
        version="0.1.0",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )

    # Order is immaterial: A64-008 decoupled the two middlewares (see
    # common/middleware.py's docstring), so neither depends on the other
    # having already run.
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)

    _configure_storage(app, get_settings())

    # Unversioned, for load-balancer and orchestrator probes: a liveness
    # check must not sit behind API versioning that could itself fail to
    # resolve (app/api/v1/health.py's docstring).
    app.include_router(health_router)
    app.include_router(api_router, prefix=API_PREFIX)

    return app
