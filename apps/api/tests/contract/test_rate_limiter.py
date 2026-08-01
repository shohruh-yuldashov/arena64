"""`RedisRateLimiter` against a real Redis 8.

There is no fake half to this suite, unlike every other file in
`tests/contract/`, and the absence is the point. The properties that
matter here — that a limit holds under concurrency, that the window
slides, that keys expire on their own — are properties of a Lua script and
of Redis, not of Python. A fake would be a second implementation of the
same algorithm, and a contract suite proving two implementations agree
proves nothing when one of them exists only to agree.

So `tests/fakes/rate_limiter.py` holds doubles that make *other* suites
independent of rate limiting, and this file holds the real behaviour.

Skipped, not failed, when Redis is unreachable (see `conftest.py`).

## The clock is movable, and that is what makes this suite fast

Every limit on the platform has a window measured in minutes or hours.
Testing that a counter resets by waiting for a 15-minute window would be a
test nobody runs. `RedisRateLimiter` takes a `Clock` port (AD-07) for
exactly this reason: "fifteen minutes later" is an assignment.

Redis's own TTLs are the one thing that cannot be faked this way — they
run on Redis's wall clock — so the expiry assertions check `PTTL` rather
than waiting for a key to vanish.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from app.config.settings import RateLimitSettings
from app.core.exceptions import TransientInfrastructureError
from app.core.rate_limiting import RateLimitRule, RateLimitScope, RateLimitSubject
from app.database.rate_limiter import RedisRateLimiter

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

IP_RULE = RateLimitRule(
    name="login_ip", scope=RateLimitScope.IP, limit=5, window=timedelta(minutes=15)
)
EMAIL_RULE = RateLimitRule(
    name="login_email", scope=RateLimitScope.EMAIL, limit=10, window=timedelta(hours=1)
)


def settings(**overrides: object) -> RateLimitSettings:
    """Rate limiting, explicitly **on**.

    `tests/conftest.py` sets `RATE_LIMIT_ENABLED=false` for the whole
    suite, so that files testing other features are not coupled through a
    shared counter with an hour-long window. A bare `RateLimitSettings()`
    here would therefore inherit `enabled=False` and every assertion in
    this file would pass against a limiter that counts nothing — the
    vacuous green this helper exists to prevent.
    """
    return RateLimitSettings(enabled=True, **overrides)  # type: ignore[arg-type]


class MovableClock:
    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def limiter(contract_redis: Redis, clock: MovableClock) -> RedisRateLimiter:
    return RedisRateLimiter(contract_redis, settings=settings(), clock=clock)


def ip(address: str = "203.0.113.7") -> list[RateLimitSubject]:
    return [RateLimitSubject(IP_RULE, address)]


def login(
    address: str = "203.0.113.7", email: str = "player@example.com"
) -> list[RateLimitSubject]:
    return [RateLimitSubject(IP_RULE, address), RateLimitSubject(EMAIL_RULE, email)]


class TestLimitReached:
    async def test_allows_up_to_the_limit(self, limiter: RedisRateLimiter) -> None:
        for _ in range(IP_RULE.limit):
            assert (await limiter.acquire(ip())).allowed is True

    async def test_refuses_the_request_after_the_limit(self, limiter: RedisRateLimiter) -> None:
        for _ in range(IP_RULE.limit):
            await limiter.acquire(ip())

        assert (await limiter.acquire(ip())).allowed is False

    async def test_stays_refused(self, limiter: RedisRateLimiter) -> None:
        """A refused request must not itself count, or the window would
        extend every time the caller retried — a limiter that punishes
        politeness and never lets anyone back in."""
        for _ in range(IP_RULE.limit + 5):
            await limiter.acquire(ip())

        assert (await limiter.acquire(ip())).allowed is False

    async def test_remaining_counts_down(self, limiter: RedisRateLimiter) -> None:
        seen = [(await limiter.acquire(ip())).remaining for _ in range(IP_RULE.limit)]

        assert seen == [4, 3, 2, 1, 0]

    async def test_the_binding_rule_is_the_one_that_refused(
        self, limiter: RedisRateLimiter
    ) -> None:
        for _ in range(IP_RULE.limit):
            await limiter.acquire(login())

        decision = await limiter.acquire(login())

        assert decision.allowed is False
        assert decision.rule.name == "login_ip"

    async def test_retry_after_is_positive_and_within_the_window(
        self, limiter: RedisRateLimiter
    ) -> None:
        for _ in range(IP_RULE.limit):
            await limiter.acquire(ip())

        decision = await limiter.acquire(ip())

        assert 0 < decision.retry_after_seconds <= IP_RULE.window.total_seconds()


class TestCounterReset:
    async def test_the_window_slides(self, limiter: RedisRateLimiter, clock: MovableClock) -> None:
        for _ in range(IP_RULE.limit):
            await limiter.acquire(ip())
        assert (await limiter.acquire(ip())).allowed is False

        clock.instant = NOW + IP_RULE.window

        assert (await limiter.acquire(ip())).allowed is True

    async def test_the_allowance_is_fully_restored(
        self, limiter: RedisRateLimiter, clock: MovableClock
    ) -> None:
        for _ in range(IP_RULE.limit):
            await limiter.acquire(ip())

        clock.instant = NOW + IP_RULE.window
        for _ in range(IP_RULE.limit):
            assert (await limiter.acquire(ip())).allowed is True

    async def test_nothing_is_restored_a_moment_early(
        self, limiter: RedisRateLimiter, clock: MovableClock
    ) -> None:
        """The window is exclusive at its far edge for the same reason
        `OneTimeToken.is_expired_at` is: an off-by-one here is an allowance
        that returns a second early, every time."""
        for _ in range(IP_RULE.limit):
            await limiter.acquire(ip())

        clock.instant = NOW + IP_RULE.window - timedelta(seconds=1)

        assert (await limiter.acquire(ip())).allowed is False

    async def test_it_slides_rather_than_resetting_in_blocks(
        self, limiter: RedisRateLimiter, clock: MovableClock
    ) -> None:
        """**The reason this is a sliding log and not a fixed window.**

        Four requests early in the window, one late. A fixed window would
        empty the whole bucket at the boundary and let five more straight
        through — ten password guesses in two seconds against a limit of
        five. A sliding window only returns the allowance the early
        requests held.
        """
        for _ in range(4):
            await limiter.acquire(ip())
        clock.instant = NOW + timedelta(minutes=14)
        await limiter.acquire(ip())

        # The four early requests have aged out; the one at minute 14 has
        # not, so exactly four slots are free rather than five.
        clock.instant = NOW + timedelta(minutes=15)
        allowed = [(await limiter.acquire(ip())).allowed for _ in range(5)]

        assert allowed == [True, True, True, True, False]


class TestDifferentSubjects:
    async def test_different_ips_are_counted_separately(self, limiter: RedisRateLimiter) -> None:
        for _ in range(IP_RULE.limit):
            await limiter.acquire(ip("203.0.113.7"))

        assert (await limiter.acquire(ip("198.51.100.4"))).allowed is True

    async def test_exhausting_one_ip_does_not_touch_another(
        self, limiter: RedisRateLimiter
    ) -> None:
        for _ in range(IP_RULE.limit + 3):
            await limiter.acquire(ip("203.0.113.7"))

        for _ in range(IP_RULE.limit):
            assert (await limiter.acquire(ip("198.51.100.4"))).allowed is True

    async def test_different_emails_are_counted_separately(self, limiter: RedisRateLimiter) -> None:
        subjects = [RateLimitSubject(EMAIL_RULE, "one@example.com")]
        for _ in range(EMAIL_RULE.limit):
            await limiter.acquire(subjects)

        other = [RateLimitSubject(EMAIL_RULE, "two@example.com")]
        assert (await limiter.acquire(other)).allowed is True

    async def test_one_email_is_bounded_across_many_ips(self, limiter: RedisRateLimiter) -> None:
        """**Credential stuffing's mirror image, and why the email rule
        exists.** A distributed attempt on one account gets a fresh per-IP
        allowance from every host it owns; only the per-email rule sees
        the whole attempt.
        """
        for index in range(EMAIL_RULE.limit):
            decision = await limiter.acquire(login(address=f"203.0.113.{index}"))
            assert decision.allowed is True

        refused = await limiter.acquire(login(address="198.51.100.99"))

        assert refused.allowed is False
        assert refused.rule.name == "login_email"

    async def test_email_matching_is_case_insensitive(self, limiter: RedisRateLimiter) -> None:
        """Otherwise every per-email limit on the platform is one shift key
        away from being doubled."""
        for _ in range(EMAIL_RULE.limit):
            await limiter.acquire([RateLimitSubject(EMAIL_RULE, "player@example.com")])

        shouted = [RateLimitSubject(EMAIL_RULE, "PLAYER@EXAMPLE.COM")]

        assert (await limiter.acquire(shouted)).allowed is False

    async def test_the_same_subject_under_two_rules_is_two_buckets(
        self, limiter: RedisRateLimiter
    ) -> None:
        register = RateLimitRule(
            name="register_ip", scope=RateLimitScope.IP, limit=3, window=timedelta(hours=1)
        )
        for _ in range(3):
            await limiter.acquire([RateLimitSubject(register, "203.0.113.7")])

        assert (await limiter.acquire(ip("203.0.113.7"))).allowed is True


class TestAllOrNothing:
    async def test_a_refused_request_does_not_consume_the_other_rule(
        self, contract_redis: Redis, limiter: RedisRateLimiter
    ) -> None:
        """The contract in `core.RateLimiter`. Charging the per-IP bucket
        for a request the per-email rule refused makes the limiter quietly
        stricter than its own configuration, in a way nobody can reproduce
        from reading it."""
        for _ in range(IP_RULE.limit):
            await limiter.acquire(login())
        before = await contract_redis.zcard(RateLimitSubject(EMAIL_RULE, "player@example.com").key)

        for _ in range(5):
            await limiter.acquire(login())

        after = await contract_redis.zcard(RateLimitSubject(EMAIL_RULE, "player@example.com").key)
        assert after == before == IP_RULE.limit

    async def test_an_allowed_request_consumes_every_rule(
        self, contract_redis: Redis, limiter: RedisRateLimiter
    ) -> None:
        await limiter.acquire(login())

        assert await contract_redis.zcard(RateLimitSubject(IP_RULE, "203.0.113.7").key) == 1
        assert (
            await contract_redis.zcard(RateLimitSubject(EMAIL_RULE, "player@example.com").key) == 1
        )


class TestConcurrency:
    async def test_exactly_the_limit_passes_under_a_burst(self, limiter: RedisRateLimiter) -> None:
        """**The property the whole Lua script exists for.**

        Twenty simultaneous requests against a limit of five. A
        read-then-write limiter lets all twenty read "four used" and all
        twenty proceed; the overshoot scales with the attack, so the
        failure appears only under exactly the conditions the limiter is
        for. One atomic script makes read-prune-count-decide-write
        indivisible.
        """
        results = await asyncio.gather(*[limiter.acquire(ip()) for _ in range(20)])

        assert sum(1 for decision in results if decision.allowed) == IP_RULE.limit

    async def test_a_burst_across_two_rules_is_still_exact(self, limiter: RedisRateLimiter) -> None:
        results = await asyncio.gather(*[limiter.acquire(login()) for _ in range(20)])

        assert sum(1 for decision in results if decision.allowed) == IP_RULE.limit

    async def test_same_millisecond_requests_are_counted_individually(
        self, contract_redis: Redis, limiter: RedisRateLimiter, clock: MovableClock
    ) -> None:
        """The clock is frozen, so every request carries an identical
        score. Without the nonce in the member, `ZADD` would overwrite by
        member and five requests would be recorded as one — a limiter that
        under-counts precisely when traffic is fastest."""
        for _ in range(IP_RULE.limit):
            await limiter.acquire(ip())

        assert await contract_redis.zcard(RateLimitSubject(IP_RULE, "203.0.113.7").key) == 5


class TestKeysExpireAutomatically:
    async def test_a_ttl_is_set_on_first_use(
        self, contract_redis: Redis, limiter: RedisRateLimiter
    ) -> None:
        """ "Keys must expire automatically" — by construction, not by a
        sweeper that can fail."""
        await limiter.acquire(ip())

        ttl = await contract_redis.pttl(RateLimitSubject(IP_RULE, "203.0.113.7").key)
        assert 0 < ttl <= IP_RULE.window_ms

    async def test_the_ttl_is_extended_on_every_write(
        self, contract_redis: Redis, limiter: RedisRateLimiter, clock: MovableClock
    ) -> None:
        key = RateLimitSubject(IP_RULE, "203.0.113.7").key
        await limiter.acquire(ip())
        await contract_redis.pexpire(key, 5_000)

        await limiter.acquire(ip())

        assert await contract_redis.pttl(key) > 5_000

    async def test_no_key_outlives_its_window(
        self, contract_redis: Redis, limiter: RedisRateLimiter
    ) -> None:
        """A key can be pruned to empty and then sit until its TTL, which
        is bounded by the window and cheaper than a `DEL` on the hot
        path."""
        await limiter.acquire(login())

        for key in await contract_redis.keys("rl:*"):
            assert 0 < await contract_redis.pttl(key) <= EMAIL_RULE.window_ms


class TestStoredKeys:
    async def test_no_key_contains_an_email_address(
        self, contract_redis: Redis, limiter: RedisRateLimiter
    ) -> None:
        """A Redis keyspace has neither the access control nor the
        retention policy of the database (§14.1), and `KEYS`, `MONITOR` and
        an RDB dump all expose key names."""
        await limiter.acquire(login(email="player@example.com"))

        keys = [key.decode() for key in await contract_redis.keys("*")]
        assert keys
        assert all("player@example.com" not in key for key in keys)
        assert all("example.com" not in key for key in keys)

    async def test_keys_are_namespaced(
        self, contract_redis: Redis, limiter: RedisRateLimiter
    ) -> None:
        """So an operator can sweep every rate limit — and only rate limits
        — with one pattern."""
        await limiter.acquire(login())

        keys = [key.decode() for key in await contract_redis.keys("*")]
        assert all(key.startswith("rl:v1:") for key in keys)

    async def test_no_value_contains_a_credential(
        self, contract_redis: Redis, limiter: RedisRateLimiter
    ) -> None:
        """The members are timestamps and nonces. Nothing derived from a
        password or a token reaches Redis."""
        await limiter.acquire(login())

        for key in await contract_redis.keys("rl:*"):
            for member in await contract_redis.zrange(key, 0, -1):
                assert b"@" not in member


class TestRedisUnavailable:
    """The failure the six endpoints must survive — a Redis that is down,
    and a Redis that is merely slow."""

    @staticmethod
    def unreachable(*, fail_open: bool) -> RedisRateLimiter:
        # RFC 2606 reserves `.invalid` so this never resolves, on any
        # machine, regardless of what happens to be listening locally.
        return RedisRateLimiter(
            Redis.from_url("redis://invalid.invalid:6379/0"),
            settings=settings(fail_open=fail_open, redis_timeout_ms=50),
            clock=MovableClock(),
        )

    async def test_fail_open_allows_the_request(self) -> None:
        decision = await self.unreachable(fail_open=True).acquire(ip())

        assert decision.allowed is True

    async def test_fail_open_reports_a_full_allowance(self) -> None:
        """Truthful: nothing is being counted, so nothing is consumed.
        A fabricated countdown would let an operator watching a dashboard
        conclude the limiter was working."""
        decision = await self.unreachable(fail_open=True).acquire(ip())

        assert decision.remaining == IP_RULE.limit

    async def test_fail_open_never_raises_a_driver_error(self) -> None:
        """`core.RateLimiter` forbids raising for an infrastructure
        failure; a caller forced to `try/except` around a limit check gets
        it subtly wrong on one of six endpoints."""
        await self.unreachable(fail_open=True).acquire(login())

    async def test_fail_closed_raises_a_transient_infrastructure_error(self) -> None:
        """Which is a 503, not a 429 — the caller did nothing wrong."""
        with pytest.raises(TransientInfrastructureError):
            await self.unreachable(fail_open=False).acquire(ip())

    async def test_the_failure_is_logged_as_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ERROR, because for as long as this fires the authentication
        endpoints are running without abuse prevention — a condition an
        operator must be paged about."""
        import logging

        with caplog.at_level(logging.ERROR):
            await self.unreachable(fail_open=True).acquire(ip())

        record = next(r for r in caplog.records if r.message == "rate_limit_unavailable")
        assert record.levelno == logging.ERROR
        assert record.rules == ["login_ip"]  # type: ignore[attr-defined]

    async def test_the_failure_log_carries_no_subject(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failure log is still a log (services.md §8.5)."""
        import logging

        with caplog.at_level(logging.DEBUG):
            await self.unreachable(fail_open=True).acquire(login(email="player@example.com"))

        assert "player@example.com" not in caplog.text
        assert "203.0.113.7" not in caplog.text

    async def test_a_slow_redis_is_treated_as_a_failure(self) -> None:
        """The common outage, and the one a naive fail-open policy misses:
        without a timeout, a Redis that is slow rather than down hangs
        every authentication request for the client's default timeout, and
        the limiter takes the platform down while being available itself.
        """

        class SlowRedis:
            def register_script(self, script: str) -> object:
                async def run(**kwargs: object) -> list[int]:
                    await asyncio.sleep(5)
                    return [1, 1, 1, 5, 1000]

                return run

        limiter = RedisRateLimiter(
            SlowRedis(),  # type: ignore[arg-type]
            settings=settings(fail_open=True, redis_timeout_ms=20),
            clock=MovableClock(),
        )

        decision = await asyncio.wait_for(limiter.acquire(ip()), timeout=1)

        assert decision.allowed is True


class TestDisabled:
    async def test_the_kill_switch_touches_no_key(self, contract_redis: Redis) -> None:
        limiter = RedisRateLimiter(
            contract_redis,
            settings=RateLimitSettings(enabled=False),
            clock=MovableClock(),
        )

        for _ in range(IP_RULE.limit + 5):
            assert (await limiter.acquire(ip())).allowed is True

        assert await contract_redis.keys("rl:*") == []


class TestNoSubjects:
    async def test_an_empty_subject_list_allows_the_request(
        self, limiter: RedisRateLimiter
    ) -> None:
        """What an endpoint whose only rule needs an email gets when the
        body has none. Raising would turn a malformed body into a 500."""
        assert (await limiter.acquire([])).allowed is True

    async def test_an_empty_subject_list_touches_no_key(
        self, contract_redis: Redis, limiter: RedisRateLimiter
    ) -> None:
        await limiter.acquire([])

        assert await contract_redis.keys("*") == []
