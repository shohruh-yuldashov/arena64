"""`RedisRateLimiter` — the sliding-window log behind `core.RateLimiter`.

Lives beside `redis.py` because that is where this platform's Redis
adapters live, and it holds the two things `core/` deliberately refuses to
know: a connection, and the Lua that makes the check atomic.

## Why the whole check is one Lua script

The naive implementation is four round trips — prune, count, decide, add —
and it is wrong in a way that only appears under the exact conditions the
limiter exists for. Between the count and the add, another request does
the same count. Both see four of five used, both add, and six requests
pass a limit of five. Under a credential-stuffing run there are not two
concurrent requests but two hundred, and the overshoot scales with the
attack.

The usual patch — `INCR` and compare — is atomic but cannot express a
sliding window, cannot report an honest `Retry-After`, and reintroduces
the boundary burst `core.rate_limiting` explains at length.

So the whole operation is one script: Redis executes it start to finish
with nothing interleaved, which makes read-prune-count-decide-write
indivisible in exactly the way the naive version is not. It is also **one
round trip for every rule on the endpoint**, which matters on a path that
runs before every login.

## Why the script is all-or-nothing across rules

`POST /auth/login` has two rules. The script checks **every** key first
and only then writes to any of them, so a request refused by the per-email
rule has not consumed the caller's per-IP allowance. Doing this as two
scripts, or two calls, charges the caller for a request that never
happened — see `core.RateLimiter` on why that makes the limiter quietly
stricter than its own configuration.

## What is stored, and how it expires

One sorted set per (rule, subject), scored by the millisecond the request
arrived:

    ZADD rl:v1:login_ip:<digest> <now_ms> <now_ms>:<nonce>

The member carries a nonce because two requests in the same millisecond
would otherwise be one member — `ZADD` overwrites by member, not by score,
so a limiter keyed on the timestamp alone silently under-counts precisely
when traffic is fastest. That is not a hypothetical: concurrent requests
landing in the same millisecond is what a flood *is*.

`PEXPIRE` is reset to the full window on every write, so a key outlives
its last request by at most one window and no sweeper is needed —
"keys must expire automatically", and they do so by construction rather
than by a job that can fail. A key can also be pruned to empty by
`ZREMRANGEBYSCORE` and then sit until its TTL; that is bounded by the same
window and is cheaper than issuing a `DEL` on the hot path.

## Why failures are swallowed here rather than raised

`core.RateLimiter` forbids raising for an infrastructure failure, and this
is where that promise is kept. Every call is bounded by
`RateLimitSettings.redis_timeout_ms` and every exception below it is
caught, because the two failure modes have to behave identically: a Redis
that is *down* and a Redis that is *slow* are the same event to a caller
waiting on a login, and only the timeout catches the second.
"""

import asyncio
import logging
import secrets
from collections.abc import Sequence
from datetime import timedelta

from redis.asyncio import Redis

from app.config.settings import RateLimitSettings
from app.core.clock import Clock
from app.core.exceptions import TransientInfrastructureError
from app.core.rate_limiting import (
    RateLimitDecision,
    RateLimitRule,
    RateLimitScope,
    RateLimitSubject,
)

logger = logging.getLogger(__name__)

#: Returns a flat array: [allowed, binding_index, count, limit, reset_ms]
#:
#: `binding_index` is 1-based into KEYS, or 0 when there were no keys.
#:
#: Two passes over the keys, and the separation is the correctness
#: property: nothing is written until every key has been found to have
#: room. See this module's docstring.
_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local nonce = ARGV[2]

local allowed = 1
local binding = 0
local binding_headroom = nil
local binding_count = 0
local binding_limit = 0
local binding_reset = 0

-- Pass 1: prune expired entries and decide. No writes to the sets'
-- membership beyond dropping entries that are already outside every
-- window under consideration.
for i = 1, #KEYS do
    local limit = tonumber(ARGV[2 * i + 1])
    local window_ms = tonumber(ARGV[2 * i + 2])
    local cutoff = now_ms - window_ms

    redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', cutoff)
    local count = redis.call('ZCARD', KEYS[i])

    -- When the window is full, the caller may retry once the oldest
    -- entry falls out of it. That instant is knowable exactly, which is
    -- the whole reason for keeping a log rather than a counter.
    local reset_ms
    if count > 0 then
        local oldest = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')
        reset_ms = (tonumber(oldest[2]) + window_ms) - now_ms
        if reset_ms < 0 then reset_ms = 0 end
    else
        reset_ms = window_ms
    end

    local headroom = limit - count
    if headroom <= 0 then
        allowed = 0
    end

    -- The binding rule is the one with the least headroom. Ties go to the
    -- first rule declared, which makes the reported headers deterministic
    -- rather than dependent on key iteration order.
    if binding_headroom == nil or headroom < binding_headroom then
        binding_headroom = headroom
        binding = i
        binding_count = count
        binding_limit = limit
        binding_reset = reset_ms
    end
end

-- Pass 2: commit, but only if every rule had room.
if allowed == 1 then
    for i = 1, #KEYS do
        local window_ms = tonumber(ARGV[2 * i + 2])
        redis.call('ZADD', KEYS[i], now_ms, now_ms .. ':' .. nonce .. ':' .. i)
        redis.call('PEXPIRE', KEYS[i], window_ms)
    end
    if binding > 0 then
        binding_count = binding_count + 1
        -- The set was empty, so this request is now the oldest entry and
        -- the window resets a full window from now. Recomputing rather
        -- than reusing pass 1's value keeps the reported reset honest for
        -- the very first request against a fresh key.
        if binding_count == 1 then
            binding_reset = tonumber(ARGV[2 * binding + 2])
        end
    end
end

return {allowed, binding, binding_count, binding_limit, binding_reset}
"""


class RedisRateLimiter:
    """Enforces `RateLimitRule`s against one Redis role.

    Constructed once per process (the client is itself a pool), not per
    request — `app/api/deps.py` hands out the shared instance.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        settings: RateLimitSettings,
        clock: Clock,
    ) -> None:
        self._redis = redis
        self._settings = settings
        self._clock = clock
        # `register_script` computes the SHA once and uses EVALSHA, falling
        # back to EVAL automatically if the script is not cached — which is
        # what makes this survive a Redis restart or a `SCRIPT FLUSH`
        # without the caller noticing.
        self._script = redis.register_script(_SCRIPT)

    async def acquire(self, subjects: Sequence[RateLimitSubject]) -> RateLimitDecision:
        if not subjects or not self._settings.enabled:
            # Not an error and not a special case worth a branch upstream:
            # an endpoint whose only rule needs an email gets an empty
            # sequence when the body has none. See the port's docstring.
            return _unlimited(subjects)

        keys = [subject.key for subject in subjects]
        now_ms = int(self._clock.now().timestamp() * 1000)

        # One nonce per call, not per key — the script appends the key
        # index, so members stay unique across keys without a second draw.
        args: list[str | int] = [now_ms, secrets.token_hex(8)]
        for subject in subjects:
            args.extend((subject.rule.limit, subject.rule.window_ms))

        try:
            raw = await asyncio.wait_for(
                self._script(keys=keys, args=args),
                timeout=self._settings.redis_timeout_ms / 1000,
            )
        except Exception as error:  # noqa: BLE001 — every failure is one outcome here
            return self._on_failure(subjects, error)

        allowed, binding, count, limit, reset_ms = (int(value) for value in raw)
        rule = subjects[binding - 1].rule if binding > 0 else subjects[0].rule

        return RateLimitDecision(
            allowed=bool(allowed),
            rule=rule,
            # Clamped at zero: a limit lowered while a bucket was full
            # would otherwise report a negative remaining, and a negative
            # `X-RateLimit-Remaining` is a header no client parses.
            remaining=max(0, limit - count),
            reset_after=timedelta(milliseconds=reset_ms),
        )

    def _on_failure(
        self, subjects: Sequence[RateLimitSubject], error: BaseException
    ) -> RateLimitDecision:
        """Applies `RateLimitSettings.fail_open`. Never lets a driver
        exception escape (services.md §7.2)."""
        # ERROR, not WARNING: for as long as this is firing, six
        # authentication endpoints are running without abuse prevention.
        # That is a condition an operator must be paged about, and the log
        # level is what makes it alertable rather than merely recorded.
        #
        # No subject, no key, no address — a failure log is still a log
        # (services.md §8.5). The rule names are safe and are what an
        # operator needs to see which endpoints are exposed.
        logger.error(
            "rate_limit_unavailable",
            extra={
                "rules": [subject.rule.name for subject in subjects],
                "fail_open": self._settings.fail_open,
                "error": type(error).__name__,
            },
            exc_info=error,
        )

        if self._settings.fail_open:
            return _unlimited(subjects)

        # Deliberately **not** `TooManyRequests`. The caller did nothing
        # wrong and 429 would tell them to back off for an hour over a
        # fault that may clear in seconds; `TransientInfrastructureError`
        # is a 503, which is both true and correctly retryable.
        raise TransientInfrastructureError(
            "Rate limiting is temporarily unavailable. Try again shortly."
        ) from error


def _unlimited(subjects: Sequence[RateLimitSubject]) -> RateLimitDecision:
    """The allow-everything decision, shaped so the HTTP layer still has
    headers to emit.

    Reports the first rule's limit with its full allowance remaining, which
    is truthful in the sense that matters — nothing is being counted, so
    nothing has been consumed. Fabricating a plausible-looking countdown
    would be worse: a client watching `X-RateLimit-Remaining` fall would
    conclude the limiter was working.
    """
    if not subjects:
        # No rule applies. A synthetic rule rather than `None` so callers
        # never branch on the absence of one; `_UNLIMITED` is generous
        # enough to be obviously not a real limit if it ever reaches a
        # header.
        return RateLimitDecision(
            allowed=True,
            rule=_NO_RULE,
            remaining=_NO_RULE.limit,
            reset_after=_NO_RULE.window,
        )

    rule = subjects[0].rule
    return RateLimitDecision(
        allowed=True,
        rule=rule,
        remaining=rule.limit,
        reset_after=rule.window,
    )


_NO_RULE = RateLimitRule(
    name="unlimited",
    scope=RateLimitScope.IP,
    limit=1_000_000,
    window=timedelta(seconds=1),
)
