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
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.database.redis import RedisPools
from app.database.session import open_session


def get_settings_dependency() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_settings_dependency)]


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request (dependency-injection.md §1.4) — opened
    here, closed here, never held for the life of a connection the way a
    WebSocket's would be (DI-02, once the gateway entrypoint exists)."""
    async with open_session(request.app.state.session_factory) as session:
        yield session


DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]


def get_redis_pools(request: Request) -> RedisPools:
    """Redis clients are themselves connection pools; unlike the database
    session there is no per-request scope to open here — the pools are
    process singletons (dependency-injection.md §1.3)."""
    pools: RedisPools = request.app.state.redis_pools
    return pools


RedisPoolsDep = Annotated[RedisPools, Depends(get_redis_pools)]
