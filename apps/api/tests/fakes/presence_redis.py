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


#: A stable instant for suites that do not move time. Matches the constant
#: the other unit suites use, so a test reading across files does not have
#: to hold two "now"s in mind.
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
