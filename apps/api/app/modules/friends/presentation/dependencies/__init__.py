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
from collections.abc import Mapping, Sequence
from typing import Annotated
from uuid import UUID

from fastapi import Depends, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import ClockDep, DbSessionDep, FriendsSettingsDep, RedisPoolsDep
from app.api.outbox_deps import EventPublisherDep
from app.database.session import open_session
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.friends.application.ports import SocialGraphCache
from app.modules.friends.application.services import (
    BlockingService,
    CachedSocialGraphReader,
    FriendRequestService,
    FriendshipService,
    PairingExclusionService,
    PresenceAudienceService,
    SocialGraphReaderService,
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
from app.modules.friends.public import PairingExclusions, SocialGraphReader
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
    session: DbSessionDep,
    clock: ClockDep,
    cache: SocialGraphCacheDep,
    events: EventPublisherDep,
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
        # A64-013.7. Built over the **same** session as the repositories
        # above, which is what puts the outbox row in the acceptance's
        # transaction rather than beside it.
        events=events,
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
    session: DbSessionDep,
    clock: ClockDep,
    cache: SocialGraphCacheDep,
    events: EventPublisherDep,
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
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


BlockingServiceDep = Annotated[BlockingService, Depends(get_blocking_service)]


def get_friendship_service(
    session: DbSessionDep,
    clock: ClockDep,
    cache: SocialGraphCacheDep,
    events: EventPublisherDep,
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
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


FriendshipServiceDep = Annotated[FriendshipService, Depends(get_friendship_service)]


class SessionScopedPairingExclusions:
    """`PairingExclusions` that opens a session per read — A64-016.7 §1.

    The same arrangement `game`'s `SessionScopedSnapshots` uses, and for the
    same reason: the caller is a WebSocket, whose request scope is the whole
    connection, so a repository resolved through `DbSessionDep` would hold
    one PostgreSQL session per open socket for as long as somebody watched a
    game.

    A block check is one indexed read and the session ends with it.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def blocked_pairs_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        async with open_session(self._session_factory) as session:
            return await PairingExclusionService(
                SqlAlchemyBlockedPlayerRepository(session)
            ).blocked_pairs_among(player_ids)


class SessionScopedSocialGraphReader:
    """`SocialGraphReader` over `friends:v1:`, for a WebSocket — A64-023.3 §7.

    The cached sibling of `SessionScopedPairingExclusions` above, and the
    difference is the whole reason it exists. That one answers a *batch*
    question and is deliberately uncached — `PairingExclusionService` says
    so, because its caller is a background pairing scan whose frequency an
    operator sets. This one answers a **per-player** question on a live
    socket, which is the shape `friends:v1:blocked:<player_id>` was built
    for and the one A64-013.5 already calls a hot path.

    ## What a read actually costs

    On a hit: **one Redis `GET`** and no database work at all. The session
    below is opened and never used — `AsyncSession` acquires a connection on
    the first statement, not on construction, so a hit borrows nothing from
    the pool.

    On a miss: that `GET`, one indexed query, and one `SET`. The entry then
    serves every later read until `BlockingService` invalidates it, which it
    does on both of the two writes that can change the answer.

    The alternative — `PairingExclusions` on the message path — is one
    indexed query *per message*, which §17 of A64-023.3 refuses without
    justification and which nothing here needs: the question a quick message
    asks is about one pair, and the per-player set already answers it.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cache: SocialGraphCache,
    ) -> None:
        self._session_factory = session_factory
        # Process-wide and not session-scoped: a Redis client is a pool, not
        # a transaction, and rebuilding one per read would be the cost this
        # class exists to avoid.
        self._cache = cache

    def _reader(self, session: AsyncSession) -> CachedSocialGraphReader:
        return CachedSocialGraphReader(
            SocialGraphReaderService(
                friendships=SqlAlchemyFriendshipRepository(session),
                blocks=SqlAlchemyBlockedPlayerRepository(session),
            ),
            self._cache,
        )

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        async with open_session(self._session_factory) as session:
            return await self._reader(session).friend_ids_among(player_id, others)

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """Everyone this player cannot interact with, in **either** direction.

        Symmetric, as the port documents: BL-1 makes a block one-directional
        and invisible, but its visibility consequence runs both ways. A
        caller therefore cannot tell *which* side placed it, which is what
        keeps this from becoming a relationship oracle.
        """
        async with open_session(self._session_factory) as session:
            return await self._reader(session).blocked_ids_for(player_id)


def get_social_graph_reader_ws(
    websocket: WebSocket, cache: Annotated[SocialGraphCache, Depends(get_social_graph_cache)]
) -> SocialGraphReader:
    """`friends`' cached block read, for a WebSocket route — A64-023.3 §7.

    Typed as the published port, so the caller holds two read methods and
    cannot place a block, lift one, or list who blocked whom.
    """
    return SessionScopedSocialGraphReader(websocket.app.state.db.session_factory, cache)


WebSocketSocialGraphReaderDep = Annotated[SocialGraphReader, Depends(get_social_graph_reader_ws)]


def get_pairing_exclusions_ws(websocket: WebSocket) -> PairingExclusions:
    """`friends`' BL-2 read, for a WebSocket route.

    Typed as the published port, so the caller holds one method: it can ask
    which of a set of players exclude each other and cannot read the block
    graph, list a player's blocks, or write one.
    """
    return SessionScopedPairingExclusions(websocket.app.state.db.session_factory)


WebSocketPairingExclusionsDep = Annotated[PairingExclusions, Depends(get_pairing_exclusions_ws)]


__all__ = [
    "BlockingServiceDep",
    "SessionScopedPairingExclusions",
    "SessionScopedSocialGraphReader",
    "WebSocketSocialGraphReaderDep",
    "get_social_graph_reader_ws",
    "WebSocketPairingExclusionsDep",
    "get_pairing_exclusions_ws",
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
