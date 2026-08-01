"""The FastAPI `Depends` bridge for `friends` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved service. It is not the container.

The graph assembled per request:

    AsyncSession                          one per request (`app.api.deps`)
      -> SqlAlchemyFriendRequestRepository
      -> SqlAlchemyFriendshipRepository
      -> FriendRequestValidator           shares the request repository
      -> SessionUnitOfWork
      -> FriendRequestService             holds both repositories
      -> FriendshipService                holds the friendship repository

**Two factories since A64-013.3**, one per service, and the split is the
capability rather than the graph — the argument `profiles`' four factories
make. `FriendRequestService` can accept a request and therefore *create* a
friendship; `FriendshipService` can list, count and end them and can do
nothing with requests at all.

## Both services share one session, and that is what makes FR-4 work

`SessionUnitOfWork(session)` is constructed per factory over the *same*
request-scoped `AsyncSession`, so a write issued by the friendship
repository inside `FriendRequestService.accept`'s unit of work is part of
that transaction. A second session would put the two writes in two
transactions and reintroduce exactly the split A64-013.3 forbids.

The validator is built here rather than inside the service, and shares the
request repository instance deliberately: both read the same relation in the
same request, and a second repository would mean a second identity map over
the same rows.
"""

import logging
from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep, FriendsSettingsDep, RedisPoolsDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.friends.application.ports import SocialGraphCache
from app.modules.friends.application.services import (
    BlockingService,
    FriendRequestService,
    FriendshipService,
    PresenceAudienceService,
)
from app.modules.friends.application.validators import FriendRequestValidator
from app.modules.friends.infrastructure.cache import (
    NoSocialGraphCache,
    RedisSocialGraphCache,
)
from app.modules.friends.infrastructure.repositories import (
    SqlAlchemyBlockedPlayerRepository,
    SqlAlchemyFriendRequestRepository,
    SqlAlchemyFriendshipRepository,
)
from app.modules.users.application.services import UserService
from app.modules.users.application.services.public_profile_service import PublicProfileService
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository

logger = logging.getLogger(__name__)


def get_social_graph_cache(pools: RedisPoolsDep, settings: FriendsSettingsDep) -> SocialGraphCache:
    """The `friends:v1:` cache, or an inert stand-in — A64-013.6.

    The **`cache` Redis role**, never `live` or `limits`: every value here
    is derived from PostgreSQL and reconstructible by definition, so
    eviction is correct rather than merely tolerable (caching.md §2). Losing
    an entry costs one query.

    `NoSocialGraphCache` is the fallback, wired by
    `FRIENDS_CACHE_ENABLED=false` — for a cache that is misbehaving. The
    platform then reads the graph from PostgreSQL on every composition,
    which is what it did before this task, so the degradation is a
    legitimate configuration rather than a stub.

    `WARNING` on the fallback because nothing in a response says the cache
    is off: the platform is simply slower.
    """
    if not settings.cache_enabled:
        logger.warning("social_graph_cache_fallback", extra={"provider": "none"})
        return NoSocialGraphCache()

    logger.debug("social_graph_cache_selected", extra={"provider": "redis"})
    return RedisSocialGraphCache(pools.cache, settings=settings)


SocialGraphCacheDep = Annotated[SocialGraphCache, Depends(get_social_graph_cache)]


def get_presence_audience_service(session: DbSessionDep) -> PresenceAudienceService:
    """Who may be told about a player's presence — A64-013.6.

    **Reachable from no endpoint**, and that is the design: A64-013.6 asks
    for the fan-out integration point and excludes the transport that would
    use it. A gateway resolves this dependency; nothing on the HTTP surface
    does.

    Registered here rather than left unwired so that the object graph is
    real — a "seam" nothing can construct is a comment, not a seam.
    """
    return PresenceAudienceService(
        friendships=SqlAlchemyFriendshipRepository(session),
        blocks=SqlAlchemyBlockedPlayerRepository(session),
    )


PresenceAudienceServiceDep = Annotated[
    PresenceAudienceService, Depends(get_presence_audience_service)
]


def get_friend_request_service(
    session: DbSessionDep, clock: ClockDep, cache: SocialGraphCacheDep
) -> FriendRequestService:
    """The friend-request use cases, assembled for this request.

    Everything is constructed here rather than injected from further out,
    which is the composition root's job (BR-6 forbids a *module* reaching
    for the container, not the root wiring a module together).

    The `Clock` is injected rather than read (AD-07): `created_at` and
    `responded_at` both come from it, and a test asserting on either must
    not have to sleep. It is also what makes the future expiry window
    testable without a real one elapsing.
    """
    requests = SqlAlchemyFriendRequestRepository(session)
    blocks = SqlAlchemyBlockedPlayerRepository(session)
    return FriendRequestService(
        requests=requests,
        # The **repository**, not `FriendshipService`. That service opens
        # transactions of its own, and calling it from inside `accept`'s
        # unit of work would produce the nested, two-transaction shape
        # A64-013.3 forbids. What acceptance needs is a write that joins the
        # caller's transaction, which is exactly what a repository is.
        friendships=SqlAlchemyFriendshipRepository(session),
        # The fourth `friends:v1:` invalidation trigger — acceptance is the
        # only way a friendship comes into existence.
        cache=cache,
        # The validator now reaches three relations: requests, blocks, and
        # `users` for the existence half of FR-2 — see
        # `_ensure_recipient_reachable` on why a blocked pair and an
        # unknown id must be indistinguishable.
        validator=FriendRequestValidator(
            requests,
            blocks=blocks,
            players=PublicProfileService(
                UserService(
                    users=SqlAlchemyUserRepository(session),
                    unit_of_work=SessionUnitOfWork(session),
                    clock=clock,
                )
            ),
        ),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


FriendRequestServiceDep = Annotated[FriendRequestService, Depends(get_friend_request_service)]


def get_blocking_service(
    session: DbSessionDep, clock: ClockDep, cache: SocialGraphCacheDep
) -> BlockingService:
    """The blocking use cases — A64-013.5.

    Holds **three** repositories, which is what makes the cascade one
    transaction: blocking writes a block, ends a friendship and voids
    pending requests, and all three must commit together or not at all.

    Repositories rather than the services that wrap them, deliberately.
    `FriendshipService` and `FriendRequestService` open transactions of
    their own; calling them from inside the cascade would produce the
    nested, multi-transaction shape a block must not have — a block that
    suppressed future contact while leaving a friendship live is the block
    silently not working.

    All three share this request's `AsyncSession`, which is what puts their
    writes in one transaction.
    """
    return BlockingService(
        blocks=SqlAlchemyBlockedPlayerRepository(session),
        friendships=SqlAlchemyFriendshipRepository(session),
        requests=SqlAlchemyFriendRequestRepository(session),
        cache=cache,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


BlockingServiceDep = Annotated[BlockingService, Depends(get_blocking_service)]


def get_friendship_service(
    session: DbSessionDep, clock: ClockDep, cache: SocialGraphCacheDep
) -> FriendshipService:
    """The friend-list use cases — A64-013.3.

    Separate from `get_friend_request_service` above even though both are
    built over the same session, for the reason every port pair on this
    platform is separate: what differs is the *capability*. This one can
    list, count and end friendships and cannot touch a request; that one can
    resolve a request and, as a consequence, create a friendship.

    A single factory returning something that did both would hand every
    route on this module the union of the two.
    """
    return FriendshipService(
        friendships=SqlAlchemyFriendshipRepository(session),
        cache=cache,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


FriendshipServiceDep = Annotated[FriendshipService, Depends(get_friendship_service)]


__all__ = [
    "BlockingServiceDep",
    "PresenceAudienceServiceDep",
    "SocialGraphCacheDep",
    "FriendRequestServiceDep",
    "FriendshipServiceDep",
    "get_blocking_service",
    "get_presence_audience_service",
    "get_social_graph_cache",
    "get_friend_request_service",
    "get_friendship_service",
]
