"""`RedisConnectionRegistry` — who is connected, and where. A64-016.2 §2.

## The keyspace

    gwconn:v2:<player_id>  ->  sorted set
                               member = "<connection_id>|<node_id>"
                               score  = expiry, epoch seconds

Registered in `caching.md` §3 (C-1), versioned (C-2), and expiring (C-3) —
per member by score, and per key by `EXPIRE`.

## What v2 changed, and why v1 could not stay

A64-016.1 stored the connection id alone. That answered "does this player
have another connection", which is all presence needed, and it could not
answer "which process holds the socket" — so nothing could route a message
to it, and that task's own known-gaps list named this as the shape change
required before cross-node delivery.

The node is packed into the **member** rather than into a second structure,
and that is the decision worth defending. The alternatives were:

    a hash `gwconn:v2:routes:<player>` beside the sorted set — two
    structures, written in one transaction and expiring on two different
    mechanisms, so a member reaped by score leaves a route the hash still
    reports. The thing that drifts is the thing that routes.

    a key per connection, `gwconn:v2:conn:<connection_id>` — one key per
    live socket rather than per player, so the fleet's key count is its
    connection count, and "how many connections does this player have"
    becomes a `SCAN`.

One sorted set keeps every property A64-016.1 argued for — self-healing by
score, one atomic transaction per operation, counts returned from the writes
— and adds the location for the cost of parsing one separator.

**Migration from v1.** None is performed and none is needed. The two
prefixes are disjoint, nothing writes v1 after this deploy, and a v1 key
holds at most `GATEWAY_CONNECTION_TTL_SECONDS` plus the margin of state —
ninety seconds and change — before Redis drops it on its own. During a
rolling deploy an old node reads and writes v1 while a new one reads and
writes v2, which means presence is computed per generation for the length of
the rollout: a player with a tab on each sees themselves online from both,
and the worst case is one redundant `is_online=True` write. That is the
degradation C-2's version segment exists to make possible, and it is why the
segment is there rather than the value being widened in place.

## Why the counts still come back from the writes

Unchanged from A64-016.1 and still the point: `register` returns the live
count including the new connection and `unregister` returns what remains,
both from one `MULTI`/`EXEC`. A separate read has a window that another
node's connect lands in, so two closing sockets can both see zero and take a
connected player offline.

## Why `cache` and not `live`

Every entry is a claim about a socket its own node still holds, so losing
the lot costs a fleet-wide reconnect rather than a lost game.
architecture.md §956 groups the connection registry with live state because
it is read on the routing hot path — that is the revisit-when, and it moves
together with presence, which shares the same posture and the same
instance.
"""

import logging
from collections.abc import Sequence
from typing import Final
from uuid import UUID

from redis.asyncio import Redis

from app.core.clock import Clock
from app.gateway.node import member_separator
from app.gateway.ports import ConnectionRoute

logger = logging.getLogger(__name__)

#: Bumped when the *structure* changes — caching.md C-2. v1 held the
#: connection id alone; v2 packs the node beside it. See this module's
#: docstring on why that is a new prefix rather than a wider value.
KEY_VERSION: Final = "v2"

_KEY_PREFIX: Final = f"gwconn:{KEY_VERSION}:"

#: How much longer the *key* lives than the longest-lived member in it.
#:
#: Redis deletes a sorted set when its last member is removed, and nothing
#: removes a member that merely expired by score — so without a key-level
#: expiry a player whose every connection lapsed leaves an empty set behind.
#: The margin means the key outlives its contents rather than racing them.
_KEY_TTL_MARGIN_SECONDS: Final = 60


class RedisConnectionRegistry:
    """`ConnectionRegistry` over the `cache` role."""

    def __init__(self, redis: Redis, *, clock: Clock) -> None:
        self._redis = redis
        self._clock = clock

    @staticmethod
    def _key(player_id: UUID) -> str:
        return f"{_KEY_PREFIX}{player_id}"

    @staticmethod
    def _member(connection_id: UUID, node_id: str) -> str:
        """One connection's registry member.

        The separator is `node.member_separator()` and `resolve_node_id`
        refuses a node name containing it — the constraint and its reason
        live together so that relaxing one fails the other.
        """
        return f"{connection_id}{member_separator()}{node_id}"

    def _now(self) -> float:
        """The clock as epoch seconds — the scores' unit.

        Injected rather than `time.time()` (AD-07), and epoch rather than a
        `datetime` because a sorted-set score is a float and converting at
        every call site is how two of them end up disagreeing.
        """
        return self._clock.now().timestamp()

    async def register(
        self, player_id: UUID, connection_id: UUID, *, node_id: str, ttl_seconds: int
    ) -> int:
        """Records an open connection with its location. Returns the live
        count including it.

        Reap, add, count, expire — one transaction, so the count is of
        exactly the state this write produced. `ZADD` on an existing member
        rescores rather than duplicating, which makes this idempotent on
        `(connection_id, node_id)` and lets the heartbeat reuse it to revive
        a lapsed entry.

        A connection that somehow moved node would appear twice, since the
        member differs. It cannot: `connection_id` is minted by the process
        that accepts the socket and never leaves it.
        """
        key = self._key(player_id)
        now = self._now()

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, "-inf", now)
            pipe.zadd(key, {self._member(connection_id, node_id): now + ttl_seconds})
            pipe.zcard(key)
            pipe.expire(key, ttl_seconds + _KEY_TTL_MARGIN_SECONDS)
            results = await pipe.execute()

        return int(results[2])

    async def unregister(self, player_id: UUID, connection_id: UUID) -> int:
        """Forgets a connection. Returns the live count of what remains.

        **Removes by connection id regardless of node**, which is why it
        reads the members rather than reconstructing one: a cleanup path
        that had to know the node would be a cleanup path that fails when
        the caller has forgotten it, and the whole guarantee here is that
        cleanup cannot fail to remove what it registered.

        Idempotent: removing an absent member removes nothing, and the
        `ZCARD` that follows still reports the truth — so a second cleanup
        cannot report `0` while another connection is open, which is
        A64-016.1 §7's requirement and A64-016.2 §8's.
        """
        key = self._key(player_id)
        now = self._now()

        # Read outside the transaction: the members are needed to build the
        # `ZREM` argument, and a member that lapses between the read and the
        # write is removed by the `ZREMRANGEBYSCORE` in the same transaction
        # anyway. The count returned still comes from inside it.
        prefix = f"{connection_id}{member_separator()}"
        stale = [member for member in await self._members(key) if member.startswith(prefix)]

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, "-inf", now)
            if stale:
                pipe.zrem(key, *stale)
            pipe.zcard(key)
            results = await pipe.execute()

        return int(results[-1])

    async def refresh(
        self, player_id: UUID, connection_id: UUID, *, node_id: str, ttl_seconds: int
    ) -> bool:
        """Extends one connection's expiry. `False` if it had already lapsed.

        `ZSCORE` before `ZADD ... XX` rather than reading `XX`'s own return,
        because the answer the caller needs is "was it still there" and `XX`
        reports how many members were *added* — which is zero both when the
        member was absent and when it was present and merely rescored.
        """
        key = self._key(player_id)
        now = self._now()
        member = self._member(connection_id, node_id)

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zscore(key, member)
            pipe.zadd(key, {member: now + ttl_seconds}, xx=True)
            pipe.expire(key, ttl_seconds + _KEY_TTL_MARGIN_SECONDS)
            results = await pipe.execute()

        score = results[0]
        return score is not None and float(score) > now

    async def active_count(self, player_id: UUID) -> int:
        """How many connections a player has open right now.

        Reaps first, so the answer excludes members left behind by a node
        that died — a read that reported lapsed entries would disagree with
        what `register` returns.
        """
        key = self._key(player_id)

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, "-inf", self._now())
            pipe.zcard(key)
            results = await pipe.execute()

        return int(results[1])

    async def routes_for(self, player_id: UUID) -> Sequence[ConnectionRoute]:
        """Every live connection this player has, and where each one is.

        `ZRANGEBYSCORE` from now to `+inf` rather than reaping and then
        reading everything: one command, and the range predicate *is* the
        liveness filter, so a lapsed member is excluded from this answer
        even before some other operation gets round to deleting it.
        """
        raw = await self._redis.zrangebyscore(
            self._key(player_id), min=self._now(), max="+inf", withscores=True
        )

        decoded = (self._decode(player_id, member, float(score)) for member, score in raw)
        return tuple(route for route in decoded if route is not None)

    async def node_for(self, player_id: UUID, connection_id: UUID) -> str | None:
        """Which node holds one connection, or `None` if it is not live."""
        for route in await self.routes_for(player_id):
            if route.connection_id == connection_id:
                return route.node_id
        return None

    async def _members(self, key: str) -> Sequence[str]:
        """Every member of one key, decoded to text.

        `zrange` over the whole set rather than a score range, because the
        one caller is `unregister` and it must reach a member whose score
        has already passed — otherwise a connection that lapsed while its
        own node was slow would be unremovable by the only code that knows
        it is gone.
        """
        return [_as_text(member) for member in await self._redis.zrange(key, 0, -1)]

    @staticmethod
    def _decode(player_id: UUID, member: bytes | str, score: float) -> ConnectionRoute | None:
        """One stored member as a route, or `None` if it cannot be read.

        Tolerant rather than raising, for the reason `RedisPresenceProvider`
        decodes tolerantly: a member this build cannot parse was written by
        a different build during a rolling deploy, and the correct outcome
        is one unroutable connection rather than an exception on a fan-out
        that has no error path. Logged at `WARNING` because it should never
        happen and would otherwise be invisible.
        """
        text = _as_text(member)
        raw_connection, separator, node_id = text.partition(member_separator())
        if not separator or not node_id:
            logger.warning("gateway_route_malformed")
            return None

        try:
            connection_id = UUID(raw_connection)
        except ValueError:
            logger.warning("gateway_route_malformed")
            return None

        return ConnectionRoute(
            player_id=player_id,
            connection_id=connection_id,
            node_id=node_id,
            expires_at=score,
        )


def _as_text(value: bytes | str) -> str:
    """Redis hands back `bytes` on a default client and `str` on a decoded
    one. Normalised once, so no call site below has to care which."""
    return value.decode("utf-8") if isinstance(value, bytes) else value


__all__ = ["KEY_VERSION", "RedisConnectionRegistry"]
