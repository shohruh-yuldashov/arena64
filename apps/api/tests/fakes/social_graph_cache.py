"""In-memory stand-ins for the `friends:v1:` cache and for the Redis client
beneath it — A64-013.6.

Two fakes at two levels, because two different things need proving.

    RecordingSocialGraphCache   a `SocialGraphCache` that stores in a dict
                                and counts what was asked of it. What the
                                *invalidation triggers* are tested against:
                                the question is whether blocking drops the
                                right keys, not how a delete reaches Redis.

    FakeCacheRedis              `GET`, `SET ... EX` and `DEL`, in a dict.
                                What `RedisSocialGraphCache` itself runs on,
                                so the JSON encoding, the key derivation and
                                the "never raises" promise are exercised
                                rather than replaced.

The split follows the rule `tests/fakes/presence_redis.py` states: fake the
infrastructure, never the thing under test. A suite that substituted
`RedisSocialGraphCache` would be asserting that a dictionary behaves like a
dictionary.

Neither models eviction. TTL is a *backstop* for invalidation failing rather
than the invalidation mechanism (caching.md C-3), so a test that relied on
expiry would be asserting the backstop instead of the mechanism — and the
mechanism is what A64-013.6 says must never leave stale state.
"""

from collections.abc import Sequence
from uuid import UUID


class RecordingSocialGraphCache:
    """A working cache that also remembers what happened to it.

    Satisfies `application.ports.SocialGraphCache`. The counters exist for
    the two properties the port's docstring makes claims about and which are
    otherwise invisible: that a hit costs no query, and that the four
    triggers invalidate *both* parties.
    """

    def __init__(self) -> None:
        self.entries: dict[str, frozenset[UUID]] = {}
        self.reads: list[str] = []
        self.writes: list[str] = []
        #: One entry per `invalidate` call, holding the players it named —
        #: so a test can assert both that it fired and who it covered.
        self.invalidations: list[tuple[UUID, ...]] = []

    async def get_ids(self, key: str) -> frozenset[UUID] | None:
        self.reads.append(key)
        return self.entries.get(key)

    async def put_ids(self, key: str, ids: frozenset[UUID]) -> None:
        self.writes.append(key)
        self.entries[key] = ids

    async def invalidate(self, player_ids: Sequence[UUID]) -> None:
        self.invalidations.append(tuple(player_ids))
        # Imports the real key builder rather than reproducing the format:
        # a fake that spelled the keyspace itself would keep passing after
        # the real one changed, which is the failure this whole file exists
        # to avoid.
        from app.modules.friends.infrastructure.cache.keys import keys_for

        for player_id in player_ids:
            for key in keys_for(player_id):
                self.entries.pop(key, None)


class FakeCacheRedis:
    """The three commands `RedisSocialGraphCache` issues, over a dict.

    Values are returned as `bytes`, like the real client:
    `create_redis_pools` does not set `decode_responses`, so a decode bug
    that only appears on bytes would survive a `str`-returning fake.

    The TTL is recorded and never enforced — see this module's docstring on
    why expiry is not what these tests are about.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        #: The `ex` argument of the last `SET` per key, so a test can assert
        #: that nothing is ever stored without one.
        self.expiries: dict[str, int | None] = {}

    async def get(self, key: str) -> bytes | None:
        value = self.values.get(key)
        return value.encode() if value is not None else None

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.values[key] = value
        self.expiries[key] = ex

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.values.pop(key, None) is not None:
                removed += 1
            self.expiries.pop(key, None)
        return removed


class UnreachableCacheRedis:
    """Every command fails, which is the only interesting failure mode.

    `RedisSocialGraphCache` promises that a read failure is a miss and that
    an invalidation failure is loud but not fatal. Both promises are
    unfalsifiable without something that breaks.
    """

    async def get(self, key: str) -> bytes | None:
        raise ConnectionError("cache is unreachable")

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        raise ConnectionError("cache is unreachable")

    async def delete(self, *keys: str) -> int:
        raise ConnectionError("cache is unreachable")
