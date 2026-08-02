"""`RedisRoomMemberStore` — which sockets are attached to a match.
A64-016.2 §6.

## The keyspace

    gwroom:v1:<match_id>   ->  sorted set, member = "<player_id>|<connection_id>"
                                            score  = expiry, epoch seconds
    gwconnroom:v1:<connection_id>  ->  set of match ids this connection is in

Registered in `caching.md` §3 (C-1), versioned (C-2), expiring (C-3).

## Why the same sorted-set shape as the connection registry

Not habit — the same three properties, for the same reasons. Scoring by
expiry means a node that dies stops refreshing and its members fall out of
every read, so an abandoned room drains without a sweeper; one structure per
question means nothing to keep in step; and returning the member set from
the write is what makes "are both players here now" a fact about the state
that write produced rather than a racy second read.

§8's "empty room expires after TTL" is therefore not a job — it is what
happens when the last member lapses and Redis drops a key whose `EXPIRE`
has passed.

## The second key, and why it is worth the write

`gwconnroom:v1:<connection_id>` is a reverse index: which rooms one
connection is in. It exists for exactly one caller — the **disconnect
path**. A socket that drops has no chance to send `room.leave`, and without
the reverse index the only way to clean up is to scan every room key, which
is `SCAN` over a keyspace proportional to live matches on every closed tab.

It is a genuine second structure and therefore a genuine drift risk, which is
bounded two ways: both keys are written in one transaction, and the forward
key is authoritative — `members_of` never reads the reverse index, so a stale
entry there causes one wasted `ZREM` and never a phantom member.

## Why `cache`

Same posture as the connection registry and presence: derived from two facts
that are durable elsewhere (the roster in `game.match`, the sockets in the
registry), expendable, and reconstructible by a reconnect. AD-19 is
satisfied because nothing competitive is here — a room holds no board, no
clock and no result.
"""

import logging
from collections.abc import Sequence
from typing import Final
from uuid import UUID

from redis.asyncio import Redis

from app.core.clock import Clock
from app.gateway.node import member_separator
from app.gateway.rooms import RoomMember

logger = logging.getLogger(__name__)

KEY_VERSION: Final = "v1"

_ROOM_PREFIX: Final = f"gwroom:{KEY_VERSION}:"
_CONNECTION_PREFIX: Final = f"gwconnroom:{KEY_VERSION}:"

#: How much longer a key lives than its longest-lived member — see
#: `RedisConnectionRegistry` on why a sorted set needs one.
_KEY_TTL_MARGIN_SECONDS: Final = 60


class RedisRoomMemberStore:
    """`RoomMemberStore` over the `cache` role."""

    def __init__(self, redis: Redis, *, clock: Clock) -> None:
        self._redis = redis
        self._clock = clock

    @staticmethod
    def _room_key(match_id: UUID) -> str:
        return f"{_ROOM_PREFIX}{match_id}"

    @staticmethod
    def _connection_key(connection_id: UUID) -> str:
        return f"{_CONNECTION_PREFIX}{connection_id}"

    @staticmethod
    def _member(member: RoomMember) -> str:
        return f"{member.player_id}{member_separator()}{member.connection_id}"

    def _now(self) -> float:
        return self._clock.now().timestamp()

    async def join(
        self, match_id: UUID, member: RoomMember, *, ttl_seconds: int
    ) -> Sequence[RoomMember]:
        """Attaches one connection and reports the room afterwards.

        Reap, add, index, read — one transaction. The final `ZRANGEBYSCORE`
        is inside it, so the members returned are exactly what this join
        produced and `both_connected` cannot be computed from a set the
        other player's join has already changed.
        """
        room_key = self._room_key(match_id)
        connection_key = self._connection_key(member.connection_id)
        now = self._now()

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(room_key, "-inf", now)
            pipe.zadd(room_key, {self._member(member): now + ttl_seconds})
            pipe.expire(room_key, ttl_seconds + _KEY_TTL_MARGIN_SECONDS)
            pipe.sadd(connection_key, str(match_id))
            pipe.expire(connection_key, ttl_seconds + _KEY_TTL_MARGIN_SECONDS)
            pipe.zrangebyscore(room_key, min=now, max="+inf")
            results = await pipe.execute()

        return self._decode_all(results[-1])

    async def leave(self, match_id: UUID, member: RoomMember) -> Sequence[RoomMember]:
        """Detaches one connection and reports what remains.

        Idempotent: `ZREM` of an absent member removes nothing and the read
        that follows still reports the truth — which is what makes §8's
        "repeated leave is idempotent" a property of the store rather than a
        check the caller could forget.
        """
        room_key = self._room_key(match_id)
        now = self._now()

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(room_key, "-inf", now)
            pipe.zrem(room_key, self._member(member))
            pipe.srem(self._connection_key(member.connection_id), str(match_id))
            pipe.zrangebyscore(room_key, min=now, max="+inf")
            results = await pipe.execute()

        return self._decode_all(results[-1])

    async def members_of(self, match_id: UUID) -> Sequence[RoomMember]:
        """Everything currently attached.

        The score range *is* the liveness filter, so a member left by a node
        that died is excluded from this answer before anything gets round to
        deleting it.
        """
        raw = await self._redis.zrangebyscore(self._room_key(match_id), min=self._now(), max="+inf")
        return self._decode_all(raw)

    async def leave_all(self, member: RoomMember) -> Sequence[UUID]:
        """Detaches one connection from every room it is in.

        The disconnect path — see this module's docstring on why the reverse
        index exists for it alone.

        Reads the index first and then removes, rather than one transaction,
        because the set of rooms is not known until it has been read and a
        Lua script for a path that touches at most a handful of keys would
        be machinery for nothing. A room joined *between* the two would keep
        its member until the TTL, which is the same outcome as a node that
        died mid-join and is what the TTL is for.
        """
        connection_key = self._connection_key(member.connection_id)
        # `redis-py` types `smembers` as returning either an awaitable or a
        # value, because the same class backs the sync and async clients.
        # The async one always returns the awaitable; the suppression is
        # about the stub's union, not about the runtime.
        raw_rooms = await self._redis.smembers(connection_key)  # type: ignore[misc]
        if not raw_rooms:
            return ()

        left: list[UUID] = []
        for raw in raw_rooms:
            try:
                match_id = UUID(_as_text(raw))
            except ValueError:
                logger.warning("gateway_room_index_malformed")
                continue
            await self.leave(match_id, member)
            left.append(match_id)

        await self._redis.delete(connection_key)
        return tuple(left)

    @classmethod
    def _decode_all(cls, raw: Sequence[bytes | str]) -> Sequence[RoomMember]:
        decoded = (cls._decode(value) for value in raw)
        return tuple(member for member in decoded if member is not None)

    @staticmethod
    def _decode(raw: bytes | str) -> RoomMember | None:
        """One stored member, or `None` if it cannot be read.

        Tolerant for the reason every decoder on this platform is: a member
        written by a different build during a rolling deploy must cost one
        unroutable connection rather than an exception on a fan-out.
        """
        text = _as_text(raw)
        raw_player, separator, raw_connection = text.partition(member_separator())
        if not separator:
            logger.warning("gateway_room_member_malformed")
            return None

        try:
            return RoomMember(player_id=UUID(raw_player), connection_id=UUID(raw_connection))
        except ValueError:
            logger.warning("gateway_room_member_malformed")
            return None


def _as_text(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


__all__ = ["KEY_VERSION", "RedisRoomMemberStore"]
