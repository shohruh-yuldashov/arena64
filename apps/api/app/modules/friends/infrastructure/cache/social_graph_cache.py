"""The two implementations of `application.ports.SocialGraphCache` —
A64-013.6.

    RedisSocialGraphCache   the real one, on the `cache` role
    NoSocialGraphCache      every read misses, every invalidation is a no-op

The choice is made once per request in the composition root and logged
there, because that is the only place that knows a *choice* was made — the
arrangement every provider pair on this platform uses.

## Correctness over hit rate, stated as three rules

A64-013.6: "cache correctness is more important than hit rate." Three
decisions follow from that and none of them is negotiable for a faster
cache:

  **A read failure is a miss, never an error.** Redis being unreachable
  degrades the platform to the database, which is where the answer came
  from before this task. It must never fail a profile render.

  **An invalidation failure is loud.** A missed delete leaves a stale
  friendship or a lifted block in effect for up to the TTL, which is a
  *correctness* problem rather than a performance one — so it logs at
  `ERROR` while a read miss logs at nothing.

  **Everything has a TTL**, even though invalidation is exhaustive
  (caching.md C-3). The TTL is not the mechanism; it is the backstop for
  the mechanism failing, and it bounds how long a bug in the four triggers
  can be wrong.

## Why sets are stored as JSON arrays rather than Redis sets

A Redis `SET` would allow `SMEMBERS` and `SINTER`, which looks like the
natural fit. It is not, for one reason that matters more than the fit: an
empty set is indistinguishable from a missing key. A player with no friends
is the *most common* state on this platform, so the cache would miss on
every read for exactly those players and fall through to the database every
time.

A JSON array stores `[]` as a real value, so "no friends" is a hit.
"""

import asyncio
import json
import logging
from collections.abc import Sequence
from uuid import UUID

from redis.asyncio import Redis

from app.config.settings import FriendsSettings
from app.modules.friends.application.ports import SocialGraphEntry
from app.modules.friends.infrastructure.cache.keys import key_for, keys_for

logger = logging.getLogger(__name__)


class RedisSocialGraphCache:
    """The social graph's cache, on the `cache` Redis role.

    Constructed per request (the client it wraps is itself a process-lifetime
    pool, so this costs two attribute assignments).

    `cache`, not `limits` or `live`, and the reasoning is caching.md §2's:
    every value here is *derived* from PostgreSQL and reconstructible by
    definition, so eviction is correct rather than merely tolerable. Losing
    an entry costs one query.
    """

    def __init__(self, redis: Redis, *, settings: FriendsSettings) -> None:
        self._redis = redis
        self._settings = settings

    async def get_ids(self, player_id: UUID, entry: SocialGraphEntry) -> frozenset[UUID] | None:
        """The cached id set, or `None` on a miss.

        **Never raises.** An unreachable or slow Redis is a miss, bounded by
        `FriendsSettings.cache_timeout_ms` — because the two failure modes
        have to behave identically: down and slow are the same event to
        somebody waiting on a profile render, and only the timeout catches
        the second.

        A malformed value is also a miss, and is logged: it means something
        other than this code wrote the key, and the safe response is to
        ignore it rather than to decode half a social graph.
        """
        try:
            raw = await asyncio.wait_for(
                self._redis.get(key_for(player_id, entry)),
                timeout=self._settings.cache_timeout_ms / 1000,
            )
        except Exception as error:  # noqa: BLE001 — a cache miss is the only outcome
            # DEBUG, not WARNING. A cache that is down degrades the platform
            # to the database, which is where every one of these answers
            # came from before this task — it is slower, not broken, and an
            # alert per read would be noise during exactly the incident an
            # operator is already handling.
            logger.debug("social_graph_cache_unavailable", extra={"error": type(error).__name__})
            return None

        if raw is None:
            return None

        try:
            decoded = json.loads(raw)
            return frozenset(UUID(value) for value in decoded)
        except (ValueError, TypeError):
            # WARNING here and not above: an unreachable cache is an
            # infrastructure event, while an undecodable value means
            # something wrote this keyspace that should not have.
            logger.warning("social_graph_cache_malformed")
            return None

    async def put_ids(self, player_id: UUID, entry: SocialGraphEntry, ids: frozenset[UUID]) -> None:
        """Stores an id set with the configured TTL.

        `SET key value EX ttl` — one command, so no sequence of crashes can
        leave a key without an expiry. The TTL is a backstop rather than the
        invalidation mechanism; see this module's docstring.

        **Never raises**, and a failure to store is not even logged at
        `WARNING`: the read that follows simply misses, which is the state
        the platform was in before the cache existed.
        """
        try:
            await asyncio.wait_for(
                self._redis.set(
                    key_for(player_id, entry),
                    json.dumps([str(value) for value in ids]),
                    ex=self._settings.cache_ttl_seconds,
                ),
                timeout=self._settings.cache_timeout_ms / 1000,
            )
        except Exception as error:  # noqa: BLE001 — a failed write is a future miss
            logger.debug("social_graph_cache_write_failed", extra={"error": type(error).__name__})

    async def invalidate(self, player_ids: Sequence[UUID]) -> None:
        """Drops every cached entry for these players.

        Called with **both** parties of whatever changed, because a
        friendship and a block are facts about a pair: accepting a request
        changes two players' friend sets, and blocking changes two players'
        block sets.

        One `DEL` for every key of every player — one round trip regardless
        of how many keys the namespace grows, because `keys_for` names them
        all.

        **Failure logs at `ERROR`**, unlike every other method here, and the
        asymmetry is the point: a missed read is slow, a missed
        *invalidation* is wrong. It leaves a removed friend visible or a
        lifted block in effect for up to the TTL, which is a correctness
        defect an operator must know about — A64-013.6: "never leave stale
        friendship state."

        Still does not raise. A block that failed to invalidate must not
        also fail to be *placed*: the database is the system of record, and
        the TTL bounds the damage.
        """
        if not player_ids:
            return

        keys = [key for player_id in player_ids for key in keys_for(player_id)]
        try:
            await asyncio.wait_for(
                self._redis.delete(*keys),
                timeout=self._settings.cache_timeout_ms / 1000,
            )
        except Exception as error:  # noqa: BLE001 — never fail the write behind it
            logger.error(
                "social_graph_cache_invalidation_failed",
                extra={"player_count": len(player_ids), "error": type(error).__name__},
                exc_info=error,
            )
            return

        # INFO: A64-013.6 asks for cache invalidation to be logged, and this
        # is a real state change rather than a read. Counts and nothing else
        # — the *contents* are a social graph, and the keys embed player ids
        # (caching.md C-6).
        logger.info("social_graph_cache_invalidated", extra={"player_count": len(player_ids)})


class NoSocialGraphCache:
    """Every read misses and every invalidation is a no-op — the fallback.

    Wired by `FRIENDS_CACHE_ENABLED=false`, which is the kill switch for a
    cache that is misbehaving. The platform then reads the social graph from
    PostgreSQL on every composition, which is exactly what it did before
    A64-013.6 — so this is a legitimate degradation and not a stub.

    **Depends on nothing**: no Redis client, no settings. A fallback that
    imported the thing it replaces would fail for the reasons it exists.
    """

    async def get_ids(self, player_id: UUID, entry: SocialGraphEntry) -> frozenset[UUID] | None:
        return None

    async def put_ids(self, player_id: UUID, entry: SocialGraphEntry, ids: frozenset[UUID]) -> None:
        return None

    async def invalidate(self, player_ids: Sequence[UUID]) -> None:
        return None
