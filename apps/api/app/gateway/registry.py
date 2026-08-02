"""`RedisConnectionRegistry` — who is connected, across the fleet.
A64-016.1 §3.

## The keyspace

    gwconn:v1:<player_id>   ->   sorted set: member = connection_id,
                                             score  = expiry epoch seconds

Registered in `caching.md` §3 (C-1), versioned (C-2), and expiring (C-3) —
both per member, by score, and per key, by `EXPIRE`.

## Why a sorted set and not a counter

`INCR` on connect and `DECR` on disconnect is smaller, faster, and wrong in
the one way that matters: **it cannot be repaired**. A gateway node that is
killed mid-deploy never runs its decrements, so the counter is permanently
too high and the player is online forever — and nothing in the system can
tell a leaked increment from a real connection.

Scoring each connection by its expiry makes the structure self-healing. Every
operation first drops members whose score has passed, so a dead node's
entries disappear on the next connect, disconnect or heartbeat by *any* node,
without coordination and without a sweeper. The count is then a `ZCARD` of
what is genuinely live rather than an accumulated belief.

That is the same argument `presence:v1:` makes for a TTL over a swept row,
applied to a value that has to hold more than one thing.

## Atomicity, and why the counts come back from the writes

`ConnectionRegistry` returns the live count from `register` and `unregister`
because the presence transition depends on it — "was I the first" and "am I
the last" — and a separate read has a window that another node's connect
lands in.

Each method is therefore a single `MULTI`/`EXEC` transaction: reap, write,
count. Redis executes it without interleaving, so exactly one caller ever
sees `1` on the way up and exactly one ever sees `0` on the way down, however
many nodes are racing.

## Why `cache` and not `live`

The registry is derived and reconstructible: every entry is a claim about a
socket that its own node still holds, and losing the lot costs a fleet-wide
reconnect rather than a lost game. architecture.md §956 groups the connection
registry with live state because it is read on the routing hot path, and that
is the revisit-when — today the only reader is presence, the volume is one
small key per connected player, and it has exactly the expendable posture the
`cache` instance is configured for. `PresenceSettings` records the same
revisit-when for the same reason, and the two would move together.

## Failure posture — propagates, unlike presence

`ConnectionRegistry` raises. A connection that could not be registered is one
nothing can route to, and A64-016.2's move delivery will read this keyspace
to find the node holding a player's socket — so a silently-dropped write here
is a player whose moves go nowhere, which is not the cosmetic degradation
C-7 is written about.
"""

from typing import Final
from uuid import UUID

from redis.asyncio import Redis

from app.core.clock import Clock

#: Bumped only when the *structure* changes — caching.md C-2. The known
#: reason it would is AD-11's channel multiplexing, which wants the node
#: identity beside the connection id and is therefore a different shape.
KEY_VERSION: Final = "v1"

_KEY_PREFIX: Final = f"gwconn:{KEY_VERSION}:"

#: How much longer the *key* lives than the longest-lived member in it.
#:
#: Without a key-level expiry, a player whose every connection lapsed leaves
#: an empty sorted set behind — Redis deletes a set when its last member is
#: removed, but nothing removes a member that merely expired by score. The
#: margin means the key outlives its contents rather than racing them.
_KEY_TTL_MARGIN_SECONDS: Final = 60


class RedisConnectionRegistry:
    """`ConnectionRegistry` over the `cache` role."""

    def __init__(self, redis: Redis, *, clock: Clock) -> None:
        self._redis = redis
        self._clock = clock

    @staticmethod
    def _key(player_id: UUID) -> str:
        return f"{_KEY_PREFIX}{player_id}"

    def _now(self) -> float:
        """The clock, as epoch seconds — the scores' unit.

        Injected rather than `time.time()` (AD-07), and epoch rather than a
        `datetime` because a sorted-set score is a float and converting at
        every call site is how two of them end up disagreeing about the
        epoch.
        """
        return self._clock.now().timestamp()

    async def register(self, player_id: UUID, connection_id: UUID, *, ttl_seconds: int) -> int:
        """Records an open connection. Returns the live count including it.

        Reap, add, count, expire — in one transaction, so the count is of
        exactly the state this write produced. `ZADD` on an existing member
        updates its score rather than adding a second, which is what makes
        this idempotent on `connection_id` and lets `_heartbeat` reuse it to
        revive a lapsed entry.
        """
        key = self._key(player_id)
        now = self._now()

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, "-inf", now)
            pipe.zadd(key, {str(connection_id): now + ttl_seconds})
            pipe.zcard(key)
            pipe.expire(key, ttl_seconds + _KEY_TTL_MARGIN_SECONDS)
            results = await pipe.execute()

        return int(results[2])

    async def unregister(self, player_id: UUID, connection_id: UUID) -> int:
        """Forgets a connection. Returns the live count of what remains.

        Idempotent: `ZREM` of an absent member removes nothing, and the
        `ZCARD` that follows still reports the truth — so a second cleanup
        cannot report `0` while another connection is open, which is
        A64-016.1 §7's requirement.
        """
        key = self._key(player_id)

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, "-inf", self._now())
            pipe.zrem(key, str(connection_id))
            pipe.zcard(key)
            results = await pipe.execute()

        return int(results[2])

    async def refresh(self, player_id: UUID, connection_id: UUID, *, ttl_seconds: int) -> bool:
        """Extends one connection's expiry. `False` if it had already lapsed.

        `ZSCORE` before `ZADD` rather than `ZADD ... XX`, because the answer
        the caller needs is "was it still there", and `XX` reports how many
        members were *added* — which is zero both when the member was absent
        and when it was present and merely rescored.
        """
        key = self._key(player_id)
        now = self._now()
        member = str(connection_id)

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
        that died. Two commands rather than a bare `ZCARD` for that reason —
        a read that reported lapsed entries would be a read that disagrees
        with what `register` returns.
        """
        key = self._key(player_id)

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, "-inf", self._now())
            pipe.zcard(key)
            results = await pipe.execute()

        return int(results[1])


__all__ = ["KEY_VERSION", "RedisConnectionRegistry"]
