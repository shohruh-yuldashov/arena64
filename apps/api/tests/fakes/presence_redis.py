"""In-memory stand-ins for the slice of `redis.asyncio.Redis` that
`RedisPresenceProvider` uses — two commands, `GET` and `SET ... PX`.

## Why a fake *client* rather than a fake provider

The rest of the presence suite runs the **real** adapter over these, which
is the same choice `tests/unit/test_profile_service.py` already makes about
the rating and statistics providers: substituting the thing under test
leaves it untested. What is faked here is Redis, not presence — so the JSON
encoding, the key derivation, the decode's strictness and the "never raises"
promise are all genuinely exercised.

Held to a narrower standard than the repository fakes beside this file, and
deliberately: those must match their real adapter's behaviour or the
application tests that run on them are worthless. This one only has to
model *expiry*, because expiry is the one Redis behaviour presence's
correctness depends on — a record that outlives its window is a player who
is online forever.

Everything else about Redis is Redis's, and
`RedisPresenceProvider`'s docstring says which parts (`SET ... PX` being one
command, last-writer-wins across nodes). Those are properties of the server
and a Python reimplementation of them would prove only that the
reimplementation agrees with itself — the same argument
`tests/fakes/rate_limiter.py` makes about the limiter's Lua.
"""

from datetime import UTC, datetime, timedelta

from app.core.clock import Clock


class MovableClock:
    """A `Clock` a test can wind forward.

    Presence is the first feature on the platform whose behaviour is
    *entirely* about the passage of time, so the fixed clock the other unit
    suites use is not enough — asserting that a record expires means moving
    past its TTL, and AD-07 exists precisely so that does not mean sleeping.
    """

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class FakePresenceRedis:
    """Two commands and a TTL, backed by a dictionary.

    Shares the clock with the adapter under test, so "the key expired" and
    "the record says it was written at" move together — a fake with its own
    notion of now would let a test pass while the two disagreed by exactly
    the interval that matters.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._values: dict[str, tuple[str, datetime]] = {}
        #: The roster (A64-013.8). Deliberately **not** expiry-aware: the
        #: real sorted set has no TTL either, and its members are removed by
        #: an explicit offline or by the sweeper — which is exactly the
        #: behaviour that makes a sweeper necessary and testable.
        self._sorted_sets: dict[str, dict[str, float]] = {}

    async def get(self, key: str) -> bytes | None:
        """The stored value, or `None` once its window has passed.

        Returns `bytes`, like the real client: `create_redis_pools` does not
        set `decode_responses`, so every value the adapter sees is encoded.
        Returning `str` here would let a decode bug through.

        Expiry is evaluated on read rather than swept, which is Redis's own
        lazy behaviour and is what makes it observable without a background
        task.
        """
        entry = self._values.get(key)
        if entry is None:
            return None

        value, expires_at = entry
        if self._clock.now() >= expires_at:
            del self._values[key]
            return None
        return value.encode()

    async def set(self, key: str, value: str, *, px: int) -> bool:
        """`SET key value PX px` — the whole record and its expiry at once.

        `px` is keyword-only and required, which is the point of modelling
        `set` rather than `hset`: a call that forgot the expiry would not
        type-check here, and the failure it stands for — a presence key with
        no TTL, so a player online forever — is the worst one this design
        has.
        """
        self._values[key] = (value, self._clock.now() + timedelta(milliseconds=px))
        return True

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        """`MGET` — the batch read behind `presence_for_many`.

        Delegates to `get` per key rather than reaching into the dict, so the
        lazy-expiry behaviour modelled there applies to a batch read exactly
        as it does to a single one. A player whose window closed is `None` in
        both, which is what the sweeper's re-check depends on.
        """
        return [await self.get(key) for key in keys]

    def pipeline(self, *, transaction: bool = True) -> "FakePipeline":
        """The batched form the adapter uses since A64-013.8.

        The record and the roster entry are written together, so this fake
        has to model a pipeline or the write it is standing in for silently
        does nothing. Returns a recorder that replays onto this instance —
        which is what a non-transactional pipeline *is*: a way to send
        several independent commands in one round trip, with no atomicity.
        """
        return FakePipeline(self)

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        """Adds or re-scores members. Returns how many were new, like Redis.

        Re-scoring rather than duplicating is the behaviour the roster
        depends on: a token refresh moves a player's deadline instead of
        leaving a second entry behind at the old one.
        """
        members = self._sorted_sets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in members)
        members.update(mapping)
        return added

    async def zrem(self, key: str, *members: str) -> int:
        entries = self._sorted_sets.get(key, {})
        return sum(1 for member in members if entries.pop(member, None) is not None)

    async def zrangebyscore(
        self, key: str, *, min: float, max: float, start: int, num: int, withscores: bool
    ) -> list[tuple[bytes, float]]:
        """Members scored within the range, lowest first, windowed.

        Returns `bytes` members like the real client, because the adapter
        decodes them — a `str`-returning fake would hide a decode bug.
        """
        entries = self._sorted_sets.get(key, {})
        due = sorted(
            ((member, score) for member, score in entries.items() if min <= score <= max),
            key=lambda item: (item[1], item[0]),
        )
        return [(member.encode(), score) for member, score in due[start : start + num]]

    def roster(self, key: str) -> dict[str, float]:
        """The sorted set as a dict — a test helper, not a Redis command."""
        return dict(self._sorted_sets.get(key, {}))

    def poison(self, key: str, value: str, *, ttl_seconds: int = 60) -> None:
        """Writes a value the adapter did not produce.

        For the decode tests: a hand-edited key, a record from a build that
        wrote a different shape, a truncated value. Not part of the Redis
        surface — a test helper, named so it reads as one at the call site.
        """
        self._values[key] = (value, self._clock.now() + timedelta(seconds=ttl_seconds))


class UnreachablePresenceRedis:
    """Every command fails, the way a down or blocked Redis fails.

    Raises `ConnectionError` rather than the adapter's translated outcome,
    unlike `BrokenRateLimiter` beside it — because here the translation *is*
    what is under test. `PresenceProvider.presence_for` promises never to
    raise, and this is what proves the promise is kept rather than intended.
    """

    async def get(self, key: str) -> bytes | None:
        raise ConnectionError("presence redis is unreachable")

    async def set(self, key: str, value: str, *, px: int) -> bool:
        raise ConnectionError("presence redis is unreachable")

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        raise ConnectionError("presence redis is unreachable")

    def pipeline(self, *, transaction: bool = True) -> "UnreachablePipeline":
        return UnreachablePipeline()

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        raise ConnectionError("presence redis is unreachable")

    async def zrem(self, key: str, *members: str) -> int:
        raise ConnectionError("presence redis is unreachable")

    async def zrangebyscore(
        self, key: str, *, min: float, max: float, start: int, num: int, withscores: bool
    ) -> list[tuple[bytes, float]]:
        raise ConnectionError("presence redis is unreachable")


class FakePipeline:
    """Buffers commands and replays them on `execute`.

    Synchronous `set`/`zadd`/`zrem` returning `self`, like redis-py's — a
    pipeline queues rather than awaits, and a fake whose methods were
    coroutines would let an adapter that forgot `execute()` pass.
    """

    def __init__(self, redis: FakePresenceRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def set(self, key: str, value: str, *, px: int) -> "FakePipeline":
        self._queued.append(("set", (key, value), {"px": px}))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> "FakePipeline":
        self._queued.append(("zadd", (key, mapping), {}))
        return self

    def zrem(self, key: str, *members: str) -> "FakePipeline":
        self._queued.append(("zrem", (key, *members), {}))
        return self

    async def execute(self) -> list[object]:
        results: list[object] = []
        for name, args, kwargs in self._queued:
            results.append(await getattr(self._redis, name)(*args, **kwargs))
        self._queued.clear()
        return results


class UnreachablePipeline:
    """Queues happily and fails on `execute`, like a real one against a dead
    server: the commands are buffered locally, so only the round trip can
    fail."""

    def set(self, key: str, value: str, *, px: int) -> "UnreachablePipeline":
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> "UnreachablePipeline":
        return self

    def zrem(self, key: str, *members: str) -> "UnreachablePipeline":
        return self

    async def execute(self) -> list[object]:
        raise ConnectionError("presence redis is unreachable")


#: A stable instant for suites that do not move time. Matches the constant
#: the other unit suites use, so a test reading across files does not have
#: to hold two "now"s in mind.
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
