"""The contract suite's application factory — A64-012.8.

Every contract suite drives the **real** `create_app()` over
`httpx.ASGITransport`. That is what makes these tests worth having: routing,
middleware, the response envelope, every exception handler, every service
and every mapper are the ones that ship. What it also means is that
`lifespan` never runs — `ASGITransport` calls the application, it does not
start it — so nothing that `lifespan` puts on `app.state` exists.

Five dependencies read `app.state`, and each one had to be redirected by
hand in every suite that touched it:

    get_db_session         `app.state.db`           -> the test's transaction
    get_rate_limiter       `app.state.rate_limiter` -> an in-memory limiter
    get_presence_provider  `app.state.redis_pools`  -> `NoPresenceProvider`
    get_presence_service   `app.state.redis_pools`  -> `NoPresenceProvider`
    get_social_graph_cache `app.state.redis_pools`  -> `NoSocialGraphCache`

`get_event_publisher` (A64-013.7) needs no override: it reads the request's
session, which is already the test's, so a contract suite writes real outbox
rows inside the transaction that is rolled back at the end of the test. That
is what makes "the accept wrote an event" assertable without a worker.

Before this module that was seven near-identical fixtures, and the third
arrived in A64-012.7 by editing six files at once. This is the shape that
does not repeat: a module that adds an `app.state` dependency adds one
parameter here, and every existing suite keeps working unchanged — which is
exactly what A64-013.6 did when it added the fourth and fifth.

## What may be overridden, and what may not

Only **infrastructure the test environment cannot provide** and
**configuration a test is deliberately varying**. There is no parameter here
for a service, a repository, a mapper or a schema, and adding one would
defeat the point — the graph under test has to be the graph that ships, or a
contract test proves nothing about the contract.

`NoPresenceProvider` and `AllowAllRateLimiter` deserve a word, because they
look like doubles and only one of them is. `NoPresenceProvider` is
*production code*, wired by `PRESENCE_ENABLED=false` in a real deployment,
so a suite running on it exercises a configuration the platform genuinely
ships. `AllowAllRateLimiter` is a true double, and it is what
`tests/conftest.py` already arranges globally with `RATE_LIMIT_ENABLED=false`
— see that file on why shared counters would otherwise couple every suite
on the platform through Redis.

## Why the app and the client are two calls

    app = build_contract_app(session)
    async with contract_client(app) as http:
        ...

Rather than one helper returning a client. Three suites genuinely need the
`FastAPI` object: `test_avatar_api.py` reads `app.state.storage`,
`test_rate_limiting_api.py` inspects the route table, and several assert
against `app.openapi()`. Hiding it would push those back to hand-rolling
what this module exists to centralise.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_db_session,
    get_presence_settings,
    get_rate_limit_settings,
    get_rate_limiter,
    get_statistics_settings,
)
from app.api.outbox_deps import get_event_publisher
from app.app_factory import create_app
from app.config.settings import PresenceSettings, RateLimitSettings, StatisticsSettings
from app.core.rate_limiting import RateLimiter
from app.modules.friends.application.ports import SocialGraphCache
from app.modules.friends.infrastructure.cache import NoSocialGraphCache
from app.modules.friends.presentation.dependencies import get_social_graph_cache
from app.modules.profiles.presentation.dependencies import get_presence_provider
from app.modules.users.application.services.presence_service import PresenceService
from app.modules.users.infrastructure.presence import NoPresenceProvider
from app.modules.users.presentation.dependencies import get_presence_service
from app.modules.users.public import PresenceProvider, PresenceRecorder
from app.platform.outbox import NoEventPublisher
from tests.fakes.rate_limiter import AllowAllRateLimiter

#: The base URL every contract client uses. A constant so that a test
#: asserting against an absolute URL — an avatar URL, a `Location` header —
#: has one place to compare against.
BASE_URL = "http://testserver"


def build_contract_app(
    session: AsyncSession,
    *,
    app: FastAPI | None = None,
    rate_limiter: RateLimiter | None = None,
    rate_limit_settings: RateLimitSettings | None = None,
    presence: PresenceProvider | None = None,
    presence_recorder: PresenceRecorder | None = None,
    social_graph_cache: SocialGraphCache | None = None,
    outbox_enabled: bool = True,
    statistics_settings: StatisticsSettings | None = None,
) -> FastAPI:
    """The production application, with `lifespan`'s state stood in for.

    `session` is the only required argument: every suite needs its queries
    to land inside the test's rolled-back transaction, and an app without it
    would open a second connection whose writes outlive the test.

    `app` accepts an application the caller built itself, for the one case
    that needs it — `test_avatar_api.py` must set `STORAGE_LOCAL_ROOT` and
    clear the settings cache *before* `create_app()` runs, because storage
    is wired in `create_app` rather than `lifespan` and the mount is part of
    the route table. Everything else lets this build it.

    Every remaining parameter is a **deliberate variation**, and the
    defaults are the ones a suite that is not testing that thing wants:

        rate_limiter          `AllowAllRateLimiter`, so limits never couple
                              one suite to another through shared counters
        rate_limit_settings   left at the environment's, which
                              `tests/conftest.py` pins to disabled
        presence              `NoPresenceProvider`, which is what
                              `PRESENCE_ENABLED=false` wires in production
        presence_recorder     `NoPresenceProvider` again — the *write* half,
                              which `auth`'s lifecycle routes hold since
                              A64-013.6. Separate from `presence` because
                              the two capabilities are separate ports, and a
                              suite asserting that signing in records
                              presence passes one object as both
        social_graph_cache    `NoSocialGraphCache`, which is what
                              `FRIENDS_CACHE_ENABLED=false` wires in
                              production. Off by default for the reason
                              rate limiting is: a cache is shared state
                              across tests, and a suite that is not
                              testing the cache must not be coupled to one
        outbox_enabled        `True`, which is `OUTBOX_ENABLED`'s default
                              and its production value. A suite passes
                              `False` only to assert what the kill switch
                              does — see `get_event_publisher`
        statistics_settings   left at the environment's, i.e. enabled and
                              reading the real projection

    Overrides are registered as plain callables rather than lambdas
    capturing loop variables, so a fixture that builds two apps in one test
    cannot accidentally share one.
    """
    application = app if app is not None else create_app()
    limiter = rate_limiter if rate_limiter is not None else AllowAllRateLimiter()
    presence_provider = presence if presence is not None else NoPresenceProvider()
    recorder = presence_recorder if presence_recorder is not None else NoPresenceProvider()
    cache = social_graph_cache if social_graph_cache is not None else NoSocialGraphCache()
    presence_service = PresenceService(recorder=recorder, provider=presence_provider)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_db_session] = _session
    application.dependency_overrides[get_rate_limiter] = lambda: limiter
    application.dependency_overrides[get_presence_provider] = lambda: presence_provider
    application.dependency_overrides[get_presence_service] = lambda: presence_service
    application.dependency_overrides[get_social_graph_cache] = lambda: cache

    # A64-013.7. Overridden only to turn the outbox *off*: the enabled path
    # is the real factory over the test's session, which is exactly what a
    # contract test should exercise.
    if not outbox_enabled:
        application.dependency_overrides[get_event_publisher] = NoEventPublisher

    # A64-013.7. **`PRESENCE_ENABLED=false` follows the inert recorder.**
    #
    # Without this the app would be internally inconsistent in a way that
    # produces real, wrong behaviour rather than merely odd wiring: the
    # recorder discards every write, so the transition check reads "nobody
    # is present" before *every* sign-in and every refresh — and emits an
    # `offline -> online` edge for all of them.
    #
    # A suite that passes a working recorder is testing presence and gets
    # the enabled configuration; every other suite gets the one that matches
    # what it was actually given.
    if presence_recorder is None:
        application.dependency_overrides[get_presence_settings] = _disabled_presence

    # The two settings sections are overridden only when a test is varying
    # them. Registering an override that returns the same value the real
    # dependency would is not neutral — it hides the case where the real one
    # stops being reachable.
    if rate_limit_settings is not None:
        application.dependency_overrides[get_rate_limit_settings] = lambda: rate_limit_settings
    if statistics_settings is not None:
        application.dependency_overrides[get_statistics_settings] = lambda: statistics_settings

    return application


@asynccontextmanager
async def contract_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An `AsyncClient` over `app`, with the overrides cleared on the way
    out.

    The clear is the reason this is a context manager rather than three
    lines in each fixture. `dependency_overrides` lives on the `FastAPI`
    object, and `create_app()` is called afresh per test everywhere on this
    suite — but `test_avatar_api.py` shares one app across a test's
    fixtures, and a suite that forgot to clear would leak a closed session
    into whatever ran next. Making it structural costs nothing and removes
    the whole failure mode.

    Cleared on exit rather than in a `finally`, deliberately: if the body
    raised, the overrides are the least interesting thing about the failure
    and clearing them first would run teardown before pytest captured the
    error. `asynccontextmanager` propagates the exception either way, and
    the app object is discarded with the test.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client
    app.dependency_overrides.clear()


def _disabled_presence() -> PresenceSettings:
    """`PRESENCE_ENABLED=false` — see `build_contract_app`.

    A module-level function rather than a lambda so the same object is
    returned for every app built in a process, and so the override reads as
    a named configuration rather than an inline literal.
    """
    return PresenceSettings(enabled=False)
