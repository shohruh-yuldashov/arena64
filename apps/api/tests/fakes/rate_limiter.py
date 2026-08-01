"""In-memory `RateLimiter`s — the doubles the rest of the suite runs on.

Unlike the repository fakes beside this file, these are **not** held to a
shared contract suite with `RedisRateLimiter`, and that is a deliberate
difference rather than a gap. A repository fake exists so application
tests can exercise real logic without a database, so it has to behave
identically or the tests are worthless. These exist for the opposite
reason: so that tests of *other* features are not coupled to a limiter at
all.

The real limiter's behaviour is tested against real Redis, in
`tests/contract/test_rate_limiter.py`, because the properties that matter —
atomicity under concurrency, the sliding window, TTL expiry — are
properties of the Lua script and of Redis, and a Python reimplementation
of them would prove only that the reimplementation agrees with itself.
"""

from collections.abc import Sequence
from datetime import timedelta

from app.core.exceptions import TransientInfrastructureError
from app.core.rate_limiting import (
    RateLimitDecision,
    RateLimitRule,
    RateLimitScope,
    RateLimitSubject,
)

_NULL_RULE = RateLimitRule(
    name="none",
    scope=RateLimitScope.IP,
    limit=1_000_000,
    window=timedelta(seconds=1),
)


def _decision(subjects: Sequence[RateLimitSubject], *, allowed: bool) -> RateLimitDecision:
    rule = subjects[0].rule if subjects else _NULL_RULE
    return RateLimitDecision(
        allowed=allowed,
        rule=rule,
        remaining=rule.limit if allowed else 0,
        reset_after=rule.window,
    )


class AllowAllRateLimiter:
    """Permits everything and records what it was asked about.

    The default double for every suite that is not testing rate limiting.
    `calls` is kept so a test can still assert *that* an endpoint consulted
    the limiter and with which rules — which is the useful half of the
    coupling, without the shared counters.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[RateLimitSubject, ...]] = []

    async def acquire(self, subjects: Sequence[RateLimitSubject]) -> RateLimitDecision:
        self.calls.append(tuple(subjects))
        return _decision(subjects, allowed=True)


class DenyAllRateLimiter:
    """Refuses everything.

    For asserting the 429 path — the status, the wire code, the headers,
    the block log — without sending the six or eleven requests it would
    otherwise take to get there, and without the test depending on the
    configured limit staying where it is.
    """

    def __init__(self, *, retry_after: timedelta = timedelta(seconds=42)) -> None:
        self._retry_after = retry_after
        self.calls: list[tuple[RateLimitSubject, ...]] = []

    async def acquire(self, subjects: Sequence[RateLimitSubject]) -> RateLimitDecision:
        self.calls.append(tuple(subjects))
        rule = subjects[0].rule if subjects else _NULL_RULE
        return RateLimitDecision(
            allowed=False,
            rule=rule,
            remaining=0,
            reset_after=self._retry_after,
        )


class BrokenRateLimiter:
    """Fails the way an unreachable Redis fails, under either policy.

    Mirrors `RedisRateLimiter._on_failure` rather than raising a driver
    error, because what the HTTP layer must handle is the *adapter's*
    contract — allow, or raise `TransientInfrastructureError` — and a test
    that raised `ConnectionError` here would be asserting against a
    translation that has already happened.
    """

    def __init__(self, *, fail_open: bool = True) -> None:
        self._fail_open = fail_open

    async def acquire(self, subjects: Sequence[RateLimitSubject]) -> RateLimitDecision:
        if self._fail_open:
            return _decision(subjects, allowed=True)
        raise TransientInfrastructureError(
            "Rate limiting is temporarily unavailable. Try again shortly."
        )
