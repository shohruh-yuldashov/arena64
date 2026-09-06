"""FastAPI's `Depends` bridge into process-lifetime singletons —
dependency-injection.md DI-01.

`Depends` is used only here, at the routing layer, to hand a route its
already-resolved dependency. It is not the container: `app.core.di.Container`
is the framework-agnostic registry that a future gateway, worker, or clock
entrypoint — none of which have an HTTP request or `app.state` — will
resolve the same singletons from. This module is the thin FastAPI-specific
half of that bridge; nothing here is reachable from those other entrypoints,
and nothing in `core/` or `database/` knows this module exists.
"""

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.api.lifecycle import ServiceLifecycle
from app.config.settings import (
    FriendsSettings,
    PresenceSettings,
    RateLimitSettings,
    Settings,
    StatisticsSettings,
    get_settings,
)
from app.core.clock import Clock, SystemClock
from app.core.rate_limiting import RateLimiter
from app.core.storage import StorageProvider
from app.database.redis import RedisPools
from app.database.session_manager import DatabaseSessionManager


def get_settings_dependency() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_settings_dependency)]


def get_clock() -> Clock:
    """The real clock in production. A test overrides this dependency with
    a fixed one rather than patching `datetime` — AD-07's whole point.

    Platform-level, beside the session and the settings, because "now" is
    not any module's property. A64-010 declared it in
    `users.presentation.dependencies` because `users` was the only module
    that existed; `auth` then imported it from there, which meant every
    `auth` dependency factory reached into another module's **private**
    presentation package for it — eleven times. R-1 says reach a module
    through its published surface, and `ClockDep` was never on one.

    Moving it here removes the import rather than legitimising it, and
    leaves exactly one `SystemClock()` construction site on the platform.
    `users.presentation.dependencies` re-exports this name so its own
    routes and the tests that override it are unaffected.
    """
    return SystemClock()


ClockDep = Annotated[Clock, Depends(get_clock)]


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request (dependency-injection.md §1.4) — opened
    here, closed here, never held for the life of a connection the way a
    WebSocket's would be (DI-02, once the gateway entrypoint exists)."""
    db: DatabaseSessionManager = request.app.state.db
    async with db.session() as session:
        yield session


DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]


def get_redis_pools(connection: HTTPConnection) -> RedisPools:
    """Redis clients are themselves connection pools; unlike the database
    session there is no per-request scope to open here — the pools are
    process singletons (dependency-injection.md §1.3).

    **`HTTPConnection`, not `Request`** — A64-020.5B. It is Starlette's
    common base of `Request` and `WebSocket`, and taking it is what lets
    one dependency serve both transports.

    This was `Request` until a browser first opened `GET /ws`, and the
    consequence was total: FastAPI injects a `WebSocket` on a WebSocket
    route, could not solve a `Request` parameter, and answered **every**
    handshake with `500` before the ticket was even read. The gateway had
    never accepted a connection.

    Nothing about the pools is request-scoped, so widening the parameter
    changes no behaviour on the HTTP path — `Request` *is* an
    `HTTPConnection`, and `.app` is on the base.
    """
    pools: RedisPools = connection.app.state.redis_pools
    return pools


RedisPoolsDep = Annotated[RedisPools, Depends(get_redis_pools)]


def get_storage_provider(request: Request) -> StorageProvider:
    """The process-lifetime object store, built in `lifespan` (A64-012.2).

    A singleton because a provider holds configuration and a connection
    pool (an S3 client will; the local one holds a resolved path), and
    neither is per-request state. Nothing here is scoped to a request, so
    dependency-injection.md §1.3's "never singleton: anything holding a
    session" does not apply.

    Platform-level, beside the database and Redis, because object storage
    is not any one module's property: `avatars` writes to it and `profiles`
    reads URLs out of it, and a provider owned by either would make the
    other depend on it.

    Typed as the **port**, never as `LocalStorageProvider`. That is what
    makes "business logic must never depend on local storage" checkable —
    a route or service annotating this dependency cannot name a concrete
    provider even by accident.
    """
    storage: StorageProvider = request.app.state.storage
    return storage


StorageProviderDep = Annotated[StorageProvider, Depends(get_storage_provider)]


def get_rate_limiter(connection: HTTPConnection) -> RateLimiter:
    """The process-lifetime limiter, built in `lifespan` (A64-011.8).

    A singleton rather than per-request, and for once the reason is not
    only cost. `RedisRateLimiter` calls `register_script` in its
    constructor, which computes the Lua script's SHA so that every check
    can use `EVALSHA` — one hash instead of shipping the script body on
    every login. Building one per request would recompute that on the
    hottest unauthenticated path on the platform and discard it.

    Safe as a singleton because it holds no session and no per-request
    state: the Redis client it wraps *is* a pool, exactly as
    `get_redis_pools` above describes (dependency-injection.md §1.3).

    `HTTPConnection` for the reason `get_redis_pools` takes one: the
    gateway rate-limits move frames, so this is resolved on a WebSocket
    route as well as an HTTP one.
    """
    limiter: RateLimiter = connection.app.state.rate_limiter
    return limiter


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def get_rate_limit_settings(settings: SettingsDep) -> RateLimitSettings:
    """The rate-limiting section alone.

    A dependency of its own so the guard in `app/api/rate_limiting.py`
    receives the section it needs rather than the whole `Settings` object
    — the same narrowing the module ports elsewhere on the platform make,
    for the same reason: a component handed everything can come to depend
    on anything.
    """
    return settings.rate_limit


RateLimitSettingsDep = Annotated[RateLimitSettings, Depends(get_rate_limit_settings)]


def get_statistics_settings(settings: SettingsDep) -> StatisticsSettings:
    """The statistics section alone — A64-012.6.

    The same narrowing `get_rate_limit_settings` makes, for the same
    reason: `profiles`' composition root needs to know whether the
    statistics store is switched on, and nothing else about the platform's
    configuration.
    """
    return settings.statistics


StatisticsSettingsDep = Annotated[StatisticsSettings, Depends(get_statistics_settings)]


def get_presence_settings(settings: SettingsDep) -> PresenceSettings:
    """The presence section alone — A64-012.7.

    The same narrowing the two above make, for the same reason: the presence
    adapter needs a TTL and a timeout, and nothing else about the platform's
    configuration. A component handed the whole `Settings` object can come to
    depend on any of it.
    """
    return settings.presence


PresenceSettingsDep = Annotated[PresenceSettings, Depends(get_presence_settings)]


def get_friends_settings(settings: SettingsDep) -> FriendsSettings:
    """The friends section alone — A64-013.3.

    The same narrowing the three above make, for the same reason:
    `profiles`' composition root needs to know whether the social graph is
    switched on, and nothing else about the platform's configuration.
    """
    return settings.friends


FriendsSettingsDep = Annotated[FriendsSettings, Depends(get_friends_settings)]


@lru_cache(maxsize=1)
def service_lifecycle() -> ServiceLifecycle:
    """This process's drain flag — A64-028.6 §9.

    Cached rather than a module global, for the reason `process_metrics()`
    gives: the sharing is visible at the call site, and a test can
    `service_lifecycle.cache_clear()` to get a process that is not draining.
    Genuinely per-process state — one flag describing this instance — which
    is the one shape `CLAUDE.md` §3.9's "stateless services" does not
    forbid, because it is not request-scoped and is not replicated.
    """
    return ServiceLifecycle()


ServiceLifecycleDep = Annotated[ServiceLifecycle, Depends(service_lifecycle)]
