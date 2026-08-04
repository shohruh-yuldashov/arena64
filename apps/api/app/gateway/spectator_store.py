"""`RedisSpectatorStore` — who is watching what. A64-016.7 §3.

## The keyspace

    gwspec:v1:<match_id>          ->  sorted set, member = "<player>|<connection>"
                                                  score  = expiry, epoch seconds
    gwspecconn:v1:<connection_id> ->  set of match ids this connection watches

The same shape as `gwroom:v1:` and deliberately so — the two answer the same
kind of question about different populations, and a second structure for it
would be a second set of expiry, idempotency and cleanup rules to get right.
See `RedisRoomMemberStore` for the full argument; the short form is that
scoring by expiry makes a dead node's subscriptions drain without a sweeper,
and the reverse index exists for the disconnect path alone.

## Why spectators are not room members

`gwroom:v1:` is the *participants'* routing scope. `both_connected` is
derived from it, the move path checks membership in it, and a spectator
appearing there would make a watched game look like one whose players are
present — and would let a spectator's connection satisfy the check that says
a move may be submitted.

The two sets are unioned at fan-out time, and only for spectator-safe events.

## Bounded by what

A subscription's TTL, and nothing else — there is no cap on how many may
watch one match. That is a real gap at scale (a popular game is the spikiest
load on this platform, architecture.md §1103) and it is honest to name it:
the bound that matters there is a *fan-out* bound rather than a storage one,
and AD-10's dedicated spectator pool is the answer rather than a set size.
Recorded in the spec rather than approximated with a number.
"""

import logging
from collections.abc import Sequence
from typing import Final
from uuid import UUID

from redis.asyncio import Redis

from app.core.clock import Clock
from app.gateway.node import member_separator
from app.gateway.spectators import SpectatorSubscription

logger = logging.getLogger(__name__)

KEY_VERSION: Final = "v1"

_MATCH_PREFIX: Final = f"gwspec:{KEY_VERSION}:"
_CONNECTION_PREFIX: Final = f"gwspecconn:{KEY_VERSION}:"

#: How much longer a key lives than its longest-lived member — the same
#: margin `gwroom:v1:` keeps, and for the same reason: Redis drops a sorted
#: set when its last member is *removed*, and nothing removes one that merely
#: expired by score.
_KEY_TTL_MARGIN_SECONDS: Final = 60


class RedisSpectatorStore:
    """`SpectatorStore` over the `cache` role."""

    def __init__(self, redis: Redis, *, clock: Clock) -> None:
        self._redis = redis
        self._clock = clock

    @staticmethod
    def _match_key(match_id: UUID) -> str:
        return f"{_MATCH_PREFIX}{match_id}"

    @staticmethod
    def _connection_key(connection_id: UUID) -> str:
        return f"{_CONNECTION_PREFIX}{connection_id}"

    @staticmethod
    def _member(subscription: SpectatorSubscription) -> str:
        return f"{subscription.player_id}{member_separator()}{subscription.connection_id}"

    def _now(self) -> float:
        return self._clock.now().timestamp()

    async def subscribe(
        self, match_id: UUID, subscription: SpectatorSubscription, *, ttl_seconds: int
    ) -> int:
        """Adds a subscription and reports the audience afterwards.

        Reap, add, index, count — one transaction, so the count is of
        exactly the state this write produced.
        """
        match_key = self._match_key(match_id)
        connection_key = self._connection_key(subscription.connection_id)
        now = self._now()

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(match_key, "-inf", now)
            pipe.zadd(match_key, {self._member(subscription): now + ttl_seconds})
            pipe.expire(match_key, ttl_seconds + _KEY_TTL_MARGIN_SECONDS)
            pipe.sadd(connection_key, str(match_id))
            pipe.expire(connection_key, ttl_seconds + _KEY_TTL_MARGIN_SECONDS)
            pipe.zcard(match_key)
            results = await pipe.execute()

        return int(results[-1])

    async def unsubscribe(self, match_id: UUID, subscription: SpectatorSubscription) -> int:
        """Removes one and reports what remains. Idempotent."""
        match_key = self._match_key(match_id)
        now = self._now()

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(match_key, "-inf", now)
            pipe.zrem(match_key, self._member(subscription))
            pipe.srem(self._connection_key(subscription.connection_id), str(match_id))
            pipe.zcard(match_key)
            results = await pipe.execute()

        return int(results[-1])

    async def routes_for(self, match_id: UUID) -> Sequence[SpectatorSubscription]:
        """Everyone watching. The score range *is* the liveness filter."""
        raw = await self._redis.zrangebyscore(
            self._match_key(match_id), min=self._now(), max="+inf"
        )
        decoded = (self._decode(entry) for entry in raw)
        return tuple(entry for entry in decoded if entry is not None)

    async def unsubscribe_all(self, subscription: SpectatorSubscription) -> Sequence[UUID]:
        """Removes one connection from every match it watches.

        The disconnect path. Reads the reverse index and then removes,
        rather than one transaction, for the reason `RoomMemberStore` gives:
        the set of matches is not known until it has been read, and a
        subscription added between the two lapses on its own TTL — the same
        outcome as a node that died mid-subscribe.
        """
        connection_key = self._connection_key(subscription.connection_id)
        raw = await self._redis.smembers(connection_key)  # type: ignore[misc]
        if not raw:
            return ()

        left: list[UUID] = []
        for entry in raw:
            try:
                match_id = UUID(_as_text(entry))
            except ValueError:
                logger.warning("spectator_index_malformed")
                continue
            await self.unsubscribe(match_id, subscription)
            left.append(match_id)

        await self._redis.delete(connection_key)
        return tuple(left)

    async def audience(self, match_id: UUID) -> int:
        """How many are watching. For an operator and a test."""
        return len(await self.routes_for(match_id))

    @staticmethod
    def _decode(raw: bytes | str) -> SpectatorSubscription | None:
        text = _as_text(raw)
        player, separator, connection = text.partition(member_separator())
        if not separator:
            logger.warning("spectator_member_malformed")
            return None

        try:
            return SpectatorSubscription(player_id=UUID(player), connection_id=UUID(connection))
        except ValueError:
            logger.warning("spectator_member_malformed")
            return None


def _as_text(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


__all__ = ["KEY_VERSION", "RedisSpectatorStore"]
