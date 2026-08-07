"""Rate limiting — the vocabulary and the port. No Redis, no FastAPI.

Framework-free by the same rule that keeps `clock.py` and `unit_of_work.py`
here (dependency-injection.md §3.2: `core/` must never contain FastAPI).
What lives in this module is *what a limit is*; `app/database/rate_limiter.py`
knows how to enforce one, and `app/api/rate_limiting.py` knows how to spend
one on an HTTP request. Neither of those two knows about the other.

That split is not ceremony. AD-09's WebSocket gateway needs per-connection
rate limiting (architecture.md §10, services.md §4.2) and has no `Request`
object to read an IP from; the Celery workers of AD-17 will need to
throttle outbound notification calls and have no HTTP layer at all. Both
can program against `RateLimiter` without inheriting the HTTP layer's
opinions about where a subject comes from.

## Why a sliding window rather than services.md's "token bucket"

services.md §4.1's request-lifecycle diagram labels this step
"Rate limiting — Redis token bucket". This implements a **sliding window
log** instead, and the deviation is deliberate.

A token bucket is defined by a refill *rate* and a burst *capacity*. The
limits this platform actually has are defined as "5 requests per 15
minutes" — a count over a window, not a rate. The two can be made to
approximate each other, but three things break in the translation:

  1. **`X-RateLimit-Reset` becomes a lie.** A bucket refills continuously,
     so there is no instant at which the caller's allowance resets; the
     honest answer is "in 180 seconds you will have one more token", which
     is not what the header means. A window has a real reset instant.
  2. **`Retry-After` becomes an estimate.** With a window log the exact
     answer is known — it is when the oldest request in the window falls
     out of it. A bucket can only divide by the refill rate.
  3. **Boundary bursts.** A *fixed* window lets 2x the limit through
     across a boundary (5 at 14:59, 5 at 15:00). A sliding log does not,
     and for a login endpoint the difference is 10 password guesses in two
     seconds versus 5.

The usual objection to a sliding log is memory: it stores one entry per
request. Here the largest limit is 30 and the rest are single digits, so a
bucket holds at most 30 members and expires within the hour. That objection
is about limits three orders of magnitude larger than these.

The diagram should be updated to say "sliding window"; it is one word in a
document whose owner is unassigned, and changing it is not this task's to
do unilaterally.

## Why subjects are hashed before they become keys

A rate-limit key is derived from an IP address or an email address. Both
are personal data under database.md §14.1, and Redis is a store with
broader access, no row-level auditing, and different retention from
PostgreSQL — `MONITOR`, `KEYS`, and an unencrypted RDB dump all expose
whatever is in a key name.

`subject_digest` is what stops `rl:v1:login_email:player@example.com` from
existing. It is **obfuscation, not encryption**, and the docstring says so
rather than overclaiming: an address is guessable, so anyone holding both
the digest and a candidate list can confirm membership. What it buys is
that a leaked key dump is not *itself* a list of registered addresses, and
that an operator running `KEYS rl:*` during an incident does not read
personal data off their terminal.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

#: Half a SHA-256, hex-encoded. 128 bits is far beyond any collision
#: concern at this cardinality — and a collision is not a security event
#: here anyway, it merely makes two subjects share a bucket. Truncating
#: halves the key size, and Redis stores keys in memory.
_DIGEST_LENGTH = 32

#: Bumped only to invalidate every live bucket at once — a limit that was
#: mistakenly configured far too low, say, where waiting out the TTL is not
#: acceptable. Cheaper and far safer than `FLUSHDB` on a shared instance.
KEY_VERSION = "v1"

KEY_PREFIX = "rl"


class RateLimitScope(StrEnum):
    """What a rule counts *per*.

    A closed enum rather than a free string because the set of things the
    platform can identify a caller by is small, deliberate, and security
    relevant — a typo'd scope that silently counted everyone into one
    bucket would look like a working rate limiter while being a global
    denial of service.
    """

    IP = "ip"
    """The caller's network address. The dimension that bounds a single
    attacker; trivially evaded by a botnet, and shared by everyone behind
    one corporate NAT. Both of those are why it is never the only rule on
    an endpoint that has a better dimension available."""

    EMAIL = "email"
    """The address in the request body. The dimension that protects one
    *account* (or one inbox) regardless of how many hosts the traffic comes
    from — which is exactly the property per-IP limiting lacks, and exactly
    what credential stuffing needs to defeat."""

    USER = "user"
    """The **authenticated** account's identifier — A64-012.5.

    The right dimension for any endpoint behind a token, and the one this
    enum lacked until now. On an authenticated route the platform knows
    precisely whose account is being written, which is strictly better than
    a network address: an office, a university or a mobile carrier NAT is
    one IP shared by thousands of people, so a per-IP limit on a settings
    endpoint throttles a crowd for one member's behaviour.

    **Never derived from a request body or header**, unlike `EMAIL`. The
    subject is the principal an authentication dependency already resolved
    and proved, which is what makes this dimension unspoofable — an
    attacker cannot rotate it without stealing another account's
    credentials, and at that point the limit is not the failure. See
    `app/api/rate_limiting.py` on how the value reaches the limiter without
    `app/api/` learning that `auth` exists.

    Distinct from `EMAIL` even though both identify an account: that one
    counts a *claimed* address on unauthenticated endpoints, where the
    whole point is to bound guessing at an account nobody has proved they
    own. This one counts a proven identity. Sharing a scope would put a
    sign-in attempt and a settings change in the same bucket.
    """

    CONNECTION = "connection"
    """One WebSocket connection — A64-016.3 §13.

    The dimension a realtime tier needs and the three above cannot express.
    Not `USER`, deliberately: a player with two tabs is two clients, and a
    shared bucket would let one tab's misbehaving loop throttle the other's
    game — which on a live board is a player losing to somebody else's bug.

    Unspoofable for the same reason `USER` is, and more directly: the
    subject is a `connection_id` this process minted when it accepted the
    socket. A client never sees it and has no field to send one in.

    **Bounded by construction.** A connection lives for one socket, so the
    keyspace is bounded by concurrent connections rather than by history —
    and each key expires with its window like every other rate-limit key.
    """


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """One limit: `limit` requests per `window`, counted per `scope`.

    Frozen because a rule is configuration, and a mutable limit is one an
    endpoint could lower for itself.
    """

    name: str
    """The bucket namespace, and the only part of the key a human reads.

    Distinct per (endpoint, scope) — `login_ip` and `login_email` are two
    rules and two buckets. Sharing a name across endpoints would make one
    endpoint's traffic consume another's allowance, which is a bug that
    presents as "sometimes login is rate limited and I do not know why".
    """

    scope: RateLimitScope
    limit: int
    window: timedelta

    def __post_init__(self) -> None:
        if self.limit < 1:
            # A limit of zero is not "no requests allowed", it is almost
            # certainly an unset environment variable — and it would take
            # the endpoint down completely. Refusing to construct makes it
            # a startup failure (DI-06) rather than a total outage.
            raise ValueError(f"rate limit {self.name!r} must allow at least one request")
        if self.window <= timedelta(0):
            raise ValueError(f"rate limit {self.name!r} must have a positive window")

    @property
    def window_ms(self) -> int:
        return int(self.window.total_seconds() * 1000)


class RateLimitProfile(StrEnum):
    """How hard the limits bite, as one value per deployment kind.

    ## The problem this exists for

    Every limit on this platform is chosen for **production**, where the
    caller is one person on one connection. Local development and the
    end-to-end suite are the opposite: one address, one after another,
    dozens of legitimate sessions a minute. A64-021.6's Playwright run was
    logging 261 `refresh_ip` rejections against a 30-per-minute rule and
    failing whichever spec lost the race — a suite that reports a product
    defect and has found none.

    The two wrong answers were both available and both rejected:

        turn the limiter off      a suite that only passes without the
                                  limiter never exercises the limiter that
                                  ships, and the first regression in it is
                                  found in production
        edit the numbers          a limit lowered "for the test run" is a
                                  limit somebody eventually deploys

    So the numbers stay exactly as they are and a **multiplier** is applied
    to them, chosen by the environment and by nothing else.

    ## Why a multiplier and not a second table of numbers

    A second table is a second thing to maintain, and the failure mode is
    that a rule added today gets a production number and no development
    one — so the person who adds it discovers the gap by being rate limited
    on their own laptop. A multiplier applies to every rule that exists,
    including the ones nobody has written yet, which is the property
    §A64-021.6.6 asks for.

    It also keeps the **shape** of every limit: the window, the scope and
    the relative severity of one endpoint against another are unchanged, so
    development behaves like production with more headroom rather than like
    a different system.

    ## Fail-closed

    `for_environment` returns `PRODUCTION` for anything it does not
    recognise. An environment nobody thought about gets production limits,
    which is the direction a mistake here must fail in — the alternative is
    a new tier silently shipping with hundred-fold allowances.
    """

    PRODUCTION = "production"
    """The numbers as written. Staging and production, and the default for
    anything unrecognised."""

    DEVELOPMENT = "development"
    """`local`. Twenty times production.

    Sized against what a developer actually does: sign in repeatedly while
    working on the sign-in screen, register a handful of throwaway accounts,
    reload a page fifty times. Twenty absorbs a working afternoon and still
    refuses a runaway loop, which is worth keeping — a script that requests
    in a tight loop should be stopped on a laptop too.
    """

    TEST = "test"
    """`test` and `ci`. A hundred times production.

    Sized against the end-to-end suite, whose whole traffic comes from one
    address: the run that motivated this was making roughly thirty session
    refreshes a minute against a thirty-per-minute rule, and a suite must
    be runnable several times in a row without a developer learning to
    clear buckets first.

    Still finite, and that matters more than the number: the limiter is the
    same limiter, the counters are the same Redis counters, and a test that
    asserts a limit fires still fires — it just has to make a hundred times
    as many requests to prove it, which is why such tests override the
    settings instead.
    """

    @property
    def multiplier(self) -> int:
        return _MULTIPLIERS[self]


#: The scaling, in one place.
#:
#: A mapping rather than a chain of `if`s, so a profile added without a
#: multiplier fails at the lookup instead of defaulting to something
#: plausible — the same reason every other closed vocabulary here is a dict.
_MULTIPLIERS: dict[RateLimitProfile, int] = {
    RateLimitProfile.PRODUCTION: 1,
    RateLimitProfile.DEVELOPMENT: 20,
    RateLimitProfile.TEST: 100,
}


def scaled(rule: RateLimitRule, profile: RateLimitProfile) -> RateLimitRule:
    """One rule as this profile applies it.

    The **window is untouched**, deliberately. Widening it too would change
    what the limit means — "20 in 15 minutes" and "20 in 5 hours" are
    different rules — where multiplying the count alone keeps the shape and
    only adds headroom.

    Returns the rule itself under `PRODUCTION`, so the production path
    allocates nothing and a caller can assert identity.
    """
    if profile is RateLimitProfile.PRODUCTION:
        return rule
    return RateLimitRule(
        name=rule.name,
        scope=rule.scope,
        limit=rule.limit * profile.multiplier,
        window=rule.window,
    )


def scaled_all(
    rules: Sequence[RateLimitRule], profile: RateLimitProfile
) -> tuple[RateLimitRule, ...]:
    """Every rule as this profile applies it — the guard's entry point."""
    return tuple(scaled(rule, profile) for rule in rules)


@dataclass(frozen=True, slots=True)
class RateLimitSubject:
    """A rule bound to the thing being counted.

    The `subject` is the raw identifying value — an IP address, an email
    address. It is hashed on the way into a key and is **never** stored,
    which is what `key` exists to guarantee: no caller assembles a key by
    hand, so no caller can assemble one with the plaintext in it.
    """

    rule: RateLimitRule
    subject: str

    @property
    def key(self) -> str:
        return f"{KEY_PREFIX}:{KEY_VERSION}:{self.rule.name}:{subject_digest(self.subject)}"


def subject_digest(subject: str) -> str:
    """The stable, non-reversible-at-a-glance form of a subject.

    Case-folded first, so `Player@Example.com` and `player@example.com`
    share a bucket. Skipping that fold is a one-character bypass of every
    per-email limit on the platform, and it is invisible in testing unless
    a test deliberately varies the case — which
    `tests/unit/test_rate_limiting.py` does.

    Not salted, deliberately. A salt would make the digest unreproducible
    across processes, and every API instance has to derive the same key for
    the same caller or the limit is per-instance rather than per-platform.
    """
    return hashlib.sha256(subject.strip().casefold().encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The verdict, plus everything the HTTP layer needs for its headers.

    Reports **one** rule even when several were evaluated: the most
    constraining one, which is the rule with the least headroom left. That
    is the only choice that produces headers a client can act on — telling
    a caller they have 9 of 10 email attempts remaining while their IP
    bucket is empty would be true and useless.
    """

    allowed: bool
    rule: RateLimitRule
    """The binding rule — the one that denied the request, or, when
    allowed, the one closest to denying the next."""

    remaining: int
    reset_after: timedelta
    """How long until the binding rule's allowance changes: for a denied
    request, until one slot frees; for an allowed one, until the oldest
    counted request falls out of the window."""

    @property
    def retry_after_seconds(self) -> int:
        """Whole seconds, rounded **up**, and never below one.

        Rounding down would advertise an instant at which the request is
        still refused, so a client that obeyed the header exactly would be
        rejected a second time and would reasonably conclude the header
        lies. Zero has the same problem and additionally reads as "retry
        immediately", which is the opposite of the instruction.
        """
        return max(1, -(-int(self.reset_after.total_seconds() * 1000) // 1000))


class RateLimiter(Protocol):
    """Spends one unit of allowance against a set of rules, atomically.

    A `Protocol`, not an ABC, so the Redis adapter and a test double
    satisfy it structurally.

    ## Why one call takes *many* subjects

    `POST /auth/login` is limited per IP **and** per email. Evaluating
    those as two sequential calls is wrong in a way that is easy to miss:
    the first call consumes a slot, the second denies, and the caller has
    been charged for a request that never happened. Do that on every
    request of a credential-stuffing run and the per-IP bucket empties at
    twice the configured rate — the limiter is stricter than its own
    configuration, in a way nobody can reproduce from reading it.

    So the contract is **all or nothing**: every rule is checked, and a
    slot is consumed in every bucket only if every rule permits it. An
    implementation that cannot do that atomically cannot satisfy this
    port.

    ## What it must never do

    Raise for an infrastructure failure. The adapter decides what an
    unreachable Redis means — see `RateLimitSettings.fail_open` — and
    returns a decision either way, because a caller that had to
    `try/except` around a rate limit check would inevitably get the
    handling subtly wrong on one of the six endpoints and leave it
    unprotected or unavailable.
    """

    async def acquire(self, subjects: Sequence[RateLimitSubject]) -> RateLimitDecision:
        """Consumes one unit against every rule, or none of them.

        An empty sequence is permitted and allows the request — it is what
        an endpoint whose only rule needs an email address gets when the
        body has none, and the alternative (raising) would turn a malformed
        request body into a 500.
        """
        ...
