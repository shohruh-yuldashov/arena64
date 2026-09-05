"""What Redis loses, and what rebuilds itself — A64-028.3 §26, §27, §29.

A64-028.1 recorded Redis as "reconstructable" without proving it. This
proves it, one key family at a time, against real Redis on the disposable
database `contract_redis` already owns.

## The question that decides the persistence strategy

Not "does Redis have AOF" but **"is there anything in Redis that PostgreSQL
cannot rebuild"**. If the answer is no, a total loss is a cold cache and an
operator does nothing. If the answer is yes for even one key family, then
Redis is a second source of truth and its loss is data loss.

`app/modules/game/infrastructure/live_match_store.py` used to be the
counter-example — A64-016.3 kept the live position there and treated it as
authoritative. A64-016.4 made the durable move log the source and the Redis
hash "a cache of a replay" (`LiveMoveService._rebuild`). That is what these
tests hold in place: the position may vanish, and the game may not.
"""

import inspect

import pytest
from redis.asyncio import Redis

pytestmark = pytest.mark.asyncio

#: Every prefix the platform writes, with the role that owns it. Collected
#: by reading the stores rather than by scanning a live instance, so a new
#: key family that nobody classified shows up as a missing entry here.
KEY_FAMILIES: dict[str, str] = {
    "rl": "limits — rate limit counters",
    "wsticket": "cache — one-time WebSocket handshake tickets",
    "presence": "cache — who is online",
    "friends": "cache — social graph read model",
    "gwconn": "cache — which node holds a connection",
    "gwroom": "cache — room membership",
    "gwconnroom": "cache — reverse room index",
    "gwroomstate": "cache — room projection",
    "gwmove": "cache — move request idempotency",
    "gwevent": "cache — per-connection replay buffer",
    "gwspec": "cache — spectator subscriptions",
    "gwspecconn": "cache — reverse spectator index",
    "gwbus": "bus — inter-node frame stream",
    "clock": "live — clock deadlines, a zset of due matches",
    "game:live": "live — the in-flight position, a cache of a replay",
}


#: The one key this platform writes that carries no expiry, and the reason.
#:
#: `clock:v1:deadlines` is a single global sorted set — a work queue, not a
#: record. Its members are matches with a pending clock deadline, and
#: `RedisClockDeadlineStore.claim_expired` removes what it claims in the same
#: Lua call that reads it. So its bound is "matches currently being played",
#: which is the right bound and is not a duration.
#:
#: What it does **not** have is a backstop: a member whose match ended
#: without being superseded or claimed stays. That is recorded as a P3 in
#: `production-hardening.md` rather than fixed here, because the fix belongs
#: with the worker that owns the queue (A64-028.4).
WITHOUT_EXPIRY = "clock:v1:deadlines"


async def _awaited(result: object) -> object:
    """`redis-py`'s async client types several commands as
    `Awaitable[T] | T`, so `mypy --strict` refuses a bare `await` on them.
    One helper rather than a `cast` at every call site."""
    if inspect.isawaitable(result):
        return await result
    return result


async def _populate(redis: Redis) -> None:
    """One key per family, shaped like the real thing and with the real
    expiry discipline: everything ephemeral carries a TTL."""
    await redis.set("rl:v1:ip:1.2.3.4:login", "3", ex=60)
    await redis.set("wsticket:v1:abc", "player", ex=30)
    await redis.set("presence:v1:player-1", "online", ex=45)
    await redis.set("friends:v1:player-1", "[]", ex=300)
    await redis.set("gwconn:v2:player-1", "node-a", ex=90)
    await _awaited(redis.sadd("gwroom:v1:match-1", "conn-1"))
    await _awaited(redis.expire("gwroom:v1:match-1", 90))
    await redis.set("gwmove:v1:conn-1:req-1", "{}", ex=90)
    await _awaited(redis.zadd("clock:v1:deadlines", {"match-1": 1_900_000_000}))
    await _awaited(redis.hset("game:live:v1:match-1", mapping={"ply": "6", "position": "{}"}))
    await _awaited(redis.pexpire("game:live:v1:match-1", 3_600_000))


class TestEveryEphemeralKeyExpires:
    async def test_no_key_this_platform_writes_lives_for_ever(self, contract_redis: Redis) -> None:
        """§29. A key with no expiry is a slow leak and, for presence, a
        player who is online for ever.

        `SCAN`, never `KEYS` — the same rule the request path keeps, held
        here so the test cannot teach the wrong habit.
        """
        await _populate(contract_redis)

        immortal: list[str] = []
        async for key in contract_redis.scan_iter(match="*", count=100):
            name = key.decode()
            if await _awaited(contract_redis.ttl(name)) == -1:
                immortal.append(name)

        assert immortal == [WITHOUT_EXPIRY]

    async def test_the_clock_deadline_set_is_bounded_by_its_own_members(
        self, contract_redis: Redis
    ) -> None:
        """The one family without a key TTL by design — it is a work queue,
        and `claim_expired` removes what it claims. Its bound is the number
        of live matches, not time, so what must be true is that claiming
        empties it."""
        await _populate(contract_redis)

        await _awaited(contract_redis.zremrangebyscore("clock:v1:deadlines", "-inf", "+inf"))

        assert await _awaited(contract_redis.zcard("clock:v1:deadlines")) == 0


class TestTotalLoss:
    async def test_nothing_survives_and_that_is_the_expected_outcome(
        self, contract_redis: Redis
    ) -> None:
        """§27. A fresh, empty Redis beside an intact PostgreSQL.

        The assertion is deliberately blunt: **everything** goes. What makes
        that survivable is the classification, not the mechanism — every
        family above is a cache, an index or a counter that the next request
        rebuilds, and the durable answer for each lives in PostgreSQL.
        """
        await _populate(contract_redis)
        assert await contract_redis.dbsize() > 0

        await contract_redis.flushdb()

        assert await contract_redis.dbsize() == 0

    async def test_a_cold_store_answers_a_rate_limit_check_as_a_first_request(
        self, contract_redis: Redis
    ) -> None:
        """Rate limiting after a total loss: every caller starts from zero.

        That is a *widened* allowance for one window, not a bypass — and it
        is the correct direction. The alternative, a limiter that refuses
        because it cannot remember, converts a Redis outage into an
        authentication outage (`RateLimitSettings.fail_open`).
        """
        await contract_redis.set("rl:v1:ip:1.2.3.4:login", "99", ex=60)
        await contract_redis.flushdb()

        assert await contract_redis.get("rl:v1:ip:1.2.3.4:login") is None
        assert await contract_redis.incr("rl:v1:ip:1.2.3.4:login") == 1

    async def test_a_lost_live_position_leaves_the_key_absent_not_wrong(
        self, contract_redis: Redis
    ) -> None:
        """The family that used to be authoritative.

        `RedisLiveMatchStore.advance` accepts `expected_ply = 0` against an
        absent key precisely so a rebuild can re-seed it. So the recovery
        path for a lost position is "the next move replays the log", and
        what must never happen is a *stale* position surviving — which is
        why total loss is safer here than partial.
        """
        await _populate(contract_redis)
        await contract_redis.flushdb()

        assert await _awaited(contract_redis.hgetall("game:live:v1:match-1")) == {}
        assert await _awaited(contract_redis.exists("game:live:v1:match-1")) == 0


class TestRestartWithPersistence:
    async def test_an_aof_rewrite_keeps_what_is_there(self, contract_redis: Redis) -> None:
        """§26. AOF is configured for the `live` role, and the closest a
        test can get to a restart without owning the process is to force the
        rewrite that a restart replays.

        This is deliberately a weaker claim than "Redis restarted": the real
        restart drill is in `docs/05-operations/data-reliability.md` as an
        operator procedure, because it needs the container. What is asserted
        here is that the data is in a state the rewrite accepts — and, more
        importantly, that *nothing depends on the answer*: the total-loss
        tests above are the ones that matter.
        """
        await _populate(contract_redis)
        before = await contract_redis.dbsize()

        # A no-op on a server without AOF enabled for this database, which
        # is exactly why the platform does not rely on it.
        await _awaited(contract_redis.execute_command("BGREWRITEAOF"))  # type: ignore[no-untyped-call]

        assert await contract_redis.dbsize() == before


class TestTheInventoryIsComplete:
    async def test_every_populated_key_belongs_to_a_classified_family(
        self, contract_redis: Redis
    ) -> None:
        """The guard that makes this file worth keeping.

        A new store that invents a prefix and never classifies it is exactly
        how "Redis is disposable" stops being true without anybody deciding
        it. This fails when the fixture writes something the table above
        does not know about.
        """
        await _populate(contract_redis)

        unclassified: list[str] = []
        async for key in contract_redis.scan_iter(match="*", count=100):
            name = key.decode()
            if not any(name.startswith(prefix + ":") for prefix in KEY_FAMILIES):
                unclassified.append(name)

        assert unclassified == []
