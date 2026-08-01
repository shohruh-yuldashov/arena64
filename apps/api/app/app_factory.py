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
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exception_handlers import register_exception_handlers
from app.api.router import api_router
from app.api.v1.health import health_router
from app.common.logging import configure_logging
from app.common.middleware import CorrelationIdMiddleware, RequestIdMiddleware
from app.config.settings import Settings, get_settings
from app.core.clock import SystemClock
from app.core.constants import API_PREFIX
from app.database.rate_limiter import RedisRateLimiter
from app.database.redis import RedisPools, create_redis_pools
from app.database.session_manager import DatabaseSessionManager
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.friends.application.ports import SocialGraphCache
from app.modules.friends.infrastructure.cache import (
    NoSocialGraphCache,
    RedisSocialGraphCache,
)
from app.modules.notifications.application.services import (
    CONSUMER_NAME,
    SUBSCRIBED_EVENT_TYPES,
    SocialNotificationDispatcher,
)
from app.modules.notifications.application.services.presence_sweeper import PresenceSweeper
from app.modules.notifications.infrastructure import (
    LoggingNotificationSink,
    PresenceSweeperWorker,
    SessionScopedNotificationHandler,
)
from app.modules.notifications.presentation.dependencies import (
    build_social_notification_dispatcher,
)
from app.modules.profiles.presentation.dependencies import build_profile_renderer
from app.modules.users.infrastructure.presence import (
    NoPresenceProvider,
    RedisPresenceProvider,
)
from app.platform.outbox import (
    OutboxEventPublisher,
    OutboxWorker,
    SqlAlchemyOutboxRepository,
)
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
            "Resolving a player **id** to a public identity — who someone *appears "
            "to be*, as opposed to `auth`, which owns proving who they *are*. A "
            "player can exist without an account (a bot seat, a guest), which is why "
            "the two are separate contexts rather than one aggregate.\n\n"
            "Deliberately thin: a handle, a display name and two avatar URLs. "
            "Everything a privacy setting governs — country, statistics, presence — "
            "is on `GET /profiles/{username}` instead, so these two routes have "
            "nothing to gate and cannot drift from the rules that profile applies.\n\n"
            "Listings cover **active accounts only**. A deactivated account does not "
            "appear in any page, which matches `GET /profiles/{username}` answering "
            "`404` for one: which handles belong to withdrawn accounts is exactly "
            "what an impersonator wants to know."
        ),
    },
    {
        "name": "profiles",
        "description": (
            "Public player profiles, read by handle and by anyone. A profile is a "
            "*composition* rather than a record: identity comes from `users`, ratings "
            "from the rating system, match counts from statistics and presence from "
            "the realtime tier.\n\n"
            "Ratings currently report placeholder values — a provisional starting "
            "rating — because no game has been played on this platform yet, and "
            "presence reports `null` for everyone because the gateway that records it "
            "does not exist yet. The **shape** is final, so a client written against it "
            "today needs no change when real values arrive.\n\n"
            "**A `null` never explains itself.** `country`, `statistics`, `is_online` "
            "and `last_seen` are all `null` both when a player has hidden them and "
            "when the platform simply has nothing to report, and the two are "
            "deliberately indistinguishable — saying which applies would answer the "
            "question the privacy setting exists to decline."
        ),
    },
    {
        "name": "profile",
        "description": (
            "Your own profile, privacy settings and preferences — reading them and "
            "changing them. Every endpoint acts on the account behind your access "
            "token; there is no path segment or body field naming an account, so "
            "another player's profile cannot be addressed here.\n\n"
            "Three groups, three endpoints, deliberately not merged. `PATCH /profile` "
            "changes what you *say about yourself* — display name, biography, "
            "country. `PATCH /profile/privacy` changes what *strangers may see*. "
            "`PATCH /profile/preferences` changes what *you* see, and is the only "
            "place your interface language and timezone can be set.\n\n"
            "**Username and email are not editable anywhere** — changing either has "
            "consequences a profile edit does not (a rename must reserve the old "
            "handle; an email change must re-prove ownership), so each gets its own "
            "flow. Your avatar is at `/profile/avatar`.\n\n"
            "**Nothing here is ever redacted.** Privacy settings govern what "
            "`GET /profiles/{username}` shows a stranger, never what you see of "
            "yourself — a control that hid a value from the person who set it would "
            "be one nobody could verify they had applied."
        ),
    },
    {
        "name": "search",
        "description": (
            "Finding players by handle or display name — the entry point to everything "
            "social.\n\n"
            "**The one profile read that requires a token.** Every other public view of "
            "a player is anonymous: if you know a handle, you can read that profile "
            "signed out. Discovering handles you do not know is different, and is what "
            "this endpoint is, so it sits behind authentication and a per-account rate "
            "limit — which together make building a directory cost an attacker a "
            "registration per budget.\n\n"
            "Results are the **same** representation `GET /profiles/{username}` "
            "returns, field for field, including every privacy behaviour: a hidden "
            "country, record or presence is `null` here exactly as it is there, and a "
            "`null` never says which of its reasons applies. Deactivated accounts "
            "appear at no rank under any term."
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
        "name": "friends",
        "description": (
            "Friend requests — the first half of the social graph (A64-013.2). "
            "Sending, listing what you have sent and received, and the three ways a "
            "request ends: accepted, declined, cancelled.\n\n"
            "**Every endpoint acts as the account behind your access token.** No "
            "path, query or body field names who is sending, accepting, declining or "
            "cancelling, so acting as somebody else is not something the API can "
            "express.\n\n"
            "Only the *recipient* may accept or decline; only the *sender* may "
            "cancel. The two are not interchangeable — they leave different history, "
            "and a future decline cooldown reads it.\n\n"
            "**Nothing is ever deleted.** `DELETE /friends/requests/{id}` resolves a "
            "request to `cancelled`; accepted, declined and cancelled rows are kept, "
            "because a request that ended is a fact with a date.\n\n"
            "A declined request is **silent to the sender**: it simply leaves their "
            "outgoing list, with no notification and no explanation.\n\n"
            "Accepting does not yet produce a friend list — that is A64-013.3, which "
            "creates the friendship in the same transaction that resolves the "
            "request."
        ),
    },
    {
        "name": "blocks",
        "description": (
            "Blocking — a platform-wide policy rather than a feature (A64-013.5).\n\n"
            "**The blocked player is never told.** Nothing notifies them, no response "
            "of theirs changes shape, and no endpoint anywhere reports being blocked. "
            "A visible block is an invitation to retaliate from a second account.\n\n"
            "Blocking takes effect everywhere at once, in one transaction: any "
            "friendship ends, any pending friend request in either direction is "
            "voided, new requests are refused, each disappears from the other's "
            "search results, and neither sees the other's presence or "
            "audience-restricted profile fields.\n\n"
            "**A block outranks every privacy setting**, including `everyone`: a "
            "player who published a field and then blocked somebody has not published "
            "it to them.\n\n"
            "The effect is symmetric even though the fact is not. A block is "
            "one-directional and only the blocker can lift it — but neither party can "
            "tell which of them placed it, which is what keeps 'am I blocked' "
            "unanswerable.\n\n"
            "**Nothing is restored on unblock.** A friendship a block ended stays "
            "ended and a voided request stays voided; the two must start again."
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


def build_outbox_worker(
    db: DatabaseSessionManager, redis_pools: RedisPools, settings: Settings
) -> OutboxWorker | None:
    """The relay worker for this process, or `None` if it does not run one.

    Assembled here rather than in `notifications` because it composes three
    modules — the social graph and the audience from `friends`, the renderer
    from `profiles`, the sink from `notifications` — and composing modules is
    the composition root's job (BR-6 forbids a *module* reaching for the
    container, not the root wiring modules together).

    The dispatcher is built **per tick**, inside the handler wrapper below,
    for the reason every service is built per request: it holds repositories,
    repositories hold a session, and a session must not outlive the unit of
    work it serves. What is long-lived here is the worker and its session
    factory, which is the same lifetime `app.state.db` has.
    """
    if not settings.outbox.worker_enabled:
        return None

    clock = SystemClock()

    # `LoggingNotificationSink` is the terminal adapter until AD-09's gateway
    # exists — A64-013.7 excludes every delivery channel. See that class on
    # why it is a seam rather than a stub.
    sink = LoggingNotificationSink()

    def dispatcher_for(session: AsyncSession) -> SocialNotificationDispatcher:
        """The consumer, over one relay tick's session.

        A closure defined here rather than a method on the handler, because
        this is the **only** place permitted to name three modules' concrete
        classes at once (BR-6: a module must not reach for the container; the
        root wiring modules together is the root's job). A64-013.8 moved it
        out of `notifications.infrastructure`, where it was three boundary
        violations that an import contract then caught.

        The cache is built per call and shared by the dispatcher's graph
        reader and its profile renderer, so a block set read while resolving
        an audience and one read while rendering are the same read.
        """
        cache: SocialGraphCache = (
            RedisSocialGraphCache(redis_pools.cache, settings=settings.friends)
            if settings.friends.cache_enabled
            else NoSocialGraphCache()
        )
        return build_social_notification_dispatcher(
            session,
            cache=cache,
            profiles=build_profile_renderer(
                session,
                pools=redis_pools,
                settings=settings,
                cache=cache,
                clock=clock,
            ),
            sink=sink,
        )

    handler = SessionScopedNotificationHandler(
        session_factory=db.session_factory,
        dispatcher_factory=dispatcher_for,
        consumer=CONSUMER_NAME,
        event_types=SUBSCRIBED_EVENT_TYPES,
    )
    return OutboxWorker(
        session_factory=db.session_factory,
        # One handler today. The list is the extension point: a second
        # consumer — moderation, audit, statistics — is an entry here and
        # its own `processed_event` partition, with nothing above it
        # changing.
        handlers=[handler],
        settings=settings.outbox,
        clock=clock,
    )


def build_presence_sweeper(
    db: DatabaseSessionManager, redis_pools: RedisPools, settings: Settings
) -> PresenceSweeperWorker | None:
    """The presence sweeper for this process, or `None` if it does not run one.

    Closes the gap A64-013.7 recorded: a player whose window expires
    unobserved produces no `PresenceOffline`, so friends see them online
    until they come back and leave again. See `PresenceSweeper`.

    Assembled here for the same reason the relay's handler is: it composes
    `users`' presence adapter with the platform outbox, and composing across
    that line is the composition root's job.

    The **same `RedisPresenceProvider` instance** satisfies the roster and the
    reader, which is not incidental — the sweeper's re-check ("is this player
    back?") is only meaningful if it reads the store the roster describes.
    """
    if not settings.presence.sweeper_enabled:
        return None

    clock = SystemClock()

    def sweeper_for(session: AsyncSession) -> PresenceSweeper:
        presence = _presence_adapter(redis_pools, settings, clock)
        return PresenceSweeper(
            roster=presence,
            presence=presence,
            events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
            unit_of_work=SessionUnitOfWork(session),
            clock=clock,
            batch_size=settings.presence.sweep_batch_size,
        )

    return PresenceSweeperWorker(
        session_factory=db.session_factory,
        sweeper_factory=sweeper_for,
        interval_seconds=settings.presence.sweep_interval_seconds,
    )


def _presence_adapter(
    redis_pools: RedisPools, settings: Settings, clock: SystemClock
) -> RedisPresenceProvider | NoPresenceProvider:
    """The presence store this process talks to.

    Branches on `PRESENCE_ENABLED` exactly as every other presence factory
    does. With presence off the sweeper reads an always-empty roster and
    ticks harmlessly — which is why the sweeper needs no kill switch for
    *that* condition, only for "does this process run it".
    """
    if not settings.presence.enabled:
        return NoPresenceProvider()
    return RedisPresenceProvider(redis_pools.cache, settings=settings.presence, clock=clock)


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

    # A64-013.7. The outbox relay, started **after** everything it needs is
    # on `app.state` and stopped **before** any of it is torn down — a tick
    # holding a session while the engine closes underneath it is the shape
    # that turns every shutdown into a confusing error.
    #
    # Started in this process only when `OUTBOX_WORKER_ENABLED` says so. The
    # intended deployment is one API tier with it off and one small worker
    # tier with it on, running the same image; the default is `true` because
    # a single-node development environment has no second tier and a feature
    # that silently never delivers is worse than one that competes for the
    # event loop. See `OutboxWorker`.
    outbox_worker = build_outbox_worker(db, redis_pools, settings)
    if outbox_worker is not None:
        await outbox_worker.start()
    else:
        logger.info("outbox_worker_disabled", extra={"reason": "configuration"})
    app.state.outbox_worker = outbox_worker

    # A64-013.8. The presence sweeper, started beside the relay and stopped
    # before it: the sweeper *produces* events the relay consumes, so on the
    # way down the producer is quiesced first and the relay is given a final
    # tick's worth of time to drain what it already claimed.
    presence_sweeper = build_presence_sweeper(db, redis_pools, settings)
    if presence_sweeper is not None:
        await presence_sweeper.start()
    else:
        logger.info("presence_sweeper_disabled", extra={"reason": "configuration"})
    app.state.presence_sweeper = presence_sweeper

    logger.info("startup_complete")
    try:
        yield
    finally:
        logger.info("shutdown_begin")
        if presence_sweeper is not None:
            await presence_sweeper.stop()
        if outbox_worker is not None:
            await outbox_worker.stop()
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
