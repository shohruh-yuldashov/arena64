"""The HTTP half of rate limiting — resolving a request to a subject,
spending its allowance, and reporting the result in headers.

Imports FastAPI on purpose, which is why it lives under `app/api/` and not
`app/core/` (dependency-injection.md §3.2). `app/core/rate_limiting.py`
holds what a limit *is*; this module holds the only three things that are
specific to HTTP: where a caller's IP comes from, where an email comes
from, and which headers say so.

## Why a route dependency and not middleware

services.md §4.1's lifecycle diagram places rate limiting in middleware,
before routing. It is a dependency instead, for a reason the diagram
predates: **half the rules on this platform are per-email**, and an email
arrives in a request body that only the routed endpoint knows the shape
of. Middleware would have to sniff every body on every path to find the
two that carry an address.

The ordering the diagram cares about is preserved anyway. FastAPI resolves
a route's dependencies *before* validating its body, so this runs ahead of
Pydantic exactly as the diagram shows — a request with a malformed body
still consumes its IP allowance, which is what stops "send garbage" from
being a free way to probe an endpoint. `tests/unit/test_rate_limiting.py`
asserts that ordering directly, because it is a property of FastAPI's
internals rather than of this code.

## Why headers are emitted on success too

`X-RateLimit-Remaining` is only useful *before* it reaches zero. A client
that learns its budget exists only at the moment it is refused cannot pace
itself, which is the entire purpose of publishing the numbers.

The values describe the **binding** rule — the one closest to refusing —
not every rule. Six headers describing two rules would require a client to
implement its own "which one bites first" logic, and would tell an
attacker exactly which dimensions an endpoint counts.

## Where the authenticated identity comes from

`RateLimitScope.USER` (A64-012.5) counts the account making the request,
and this module deliberately cannot resolve that. It never reads a header
or a body for it — that would make the dimension spoofable — and it never
imports `auth`'s `CurrentUser`, because `app/api/` importing a module's
presentation layer is the boundary this file's location exists to keep.

Instead the *caller* supplies it: `RateLimit.enforce` takes a `principal`,
and a module's own `rate_limits.py` — which may import `CurrentUser` — is
where the two meet. `__call__` stays the unauthenticated path, so a
`USER`-scoped guard attached with a bare `Depends` fails loudly rather
than counting nothing.

## What is deliberately not here

No `X-RateLimit-Policy`, no rule names on the wire. Naming the dimension
that refused a request is the one piece of information needed to evade it:
"per email" says rotate the address, "per IP" says rotate the host. The
numbers are published; the shape of the defence is not.
"""

import logging
from collections.abc import Callable, Sequence

from fastapi import Request, Response

from app.api.deps import RateLimiterDep, RateLimitSettingsDep
from app.config.settings import RateLimitSettings
from app.core.exceptions import TooManyRequests
from app.core.rate_limiting import (
    RateLimitDecision,
    RateLimiter,
    RateLimitRule,
    RateLimitScope,
    RateLimitSubject,
)

logger = logging.getLogger(__name__)

RATE_LIMIT_LIMIT_HEADER = "X-RateLimit-Limit"
RATE_LIMIT_REMAINING_HEADER = "X-RateLimit-Remaining"
RATE_LIMIT_RESET_HEADER = "X-RateLimit-Reset"
RETRY_AFTER_HEADER = "Retry-After"

FORWARDED_FOR_HEADER = "X-Forwarded-For"

#: The bucket every caller whose address cannot be determined shares.
#:
#: A single shared bucket is the deliberately *strict* choice: the
#: alternative — treating an unknown peer as exempt — would make "arrive
#: without a resolvable address" a bypass. In practice this is reachable
#: only through an ASGI transport that sets no client scope, which is a
#: test harness rather than a deployment.
UNKNOWN_CLIENT = "unknown"


def client_ip(request: Request, *, trusted_proxy_count: int) -> str:
    """The caller's address, as far as this process can honestly tell.

    With `trusted_proxy_count == 0` the socket peer is used and
    `X-Forwarded-For` is **ignored**, because a header any client can set
    is not an identity — trusting it without a proxy in front is a rate
    limiter with a documented bypass.

    With a count of N, the address is taken N entries from the *right* of
    `X-Forwarded-For`. Each trusted proxy appends exactly one entry, so
    that position holds what the outermost trusted proxy actually observed;
    anything further left was supplied by the caller and is never read. A
    header with fewer entries than the trusted chain cannot have been
    written by that chain, so it is discarded entirely rather than
    partially believed.

    See `RateLimitSettings.trusted_proxy_count` on why both wrong values
    are severe.
    """
    if trusted_proxy_count > 0:
        forwarded = request.headers.get(FORWARDED_FOR_HEADER)
        if forwarded:
            hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
            index = len(hops) - trusted_proxy_count
            if 0 <= index < len(hops):
                return hops[index]

    return request.client.host if request.client else UNKNOWN_CLIENT


async def _email_from_body(request: Request) -> str | None:
    """The `email` field, read defensively from a body nothing has
    validated yet.

    Every failure returns `None` rather than raising, and each one is an
    ordinary request rather than an anomaly: a malformed body, a body that
    is a JSON array, a missing field, an `email` that is a number. All of
    them are about to be rejected by Pydantic with a 422 — this function's
    only job is to avoid turning that 422 into a 500 on the way.

    `request.json()` caches the raw body on the request object, so the
    endpoint's own model parses the same bytes rather than reading a
    consumed stream. That caching is what makes reading the body here
    safe at all.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — malformed input is not exceptional here
        return None

    if not isinstance(payload, dict):
        return None

    email = payload.get("email")
    return email if isinstance(email, str) and email.strip() else None


async def resolve_subjects(
    request: Request,
    rules: Sequence[RateLimitRule],
    *,
    settings: RateLimitSettings,
    principal: str | None = None,
) -> list[RateLimitSubject]:
    """Binds each rule to the value it counts, dropping the ones that have
    nothing to count.

    A dropped rule is not a failure. `POST /auth/login` with no `email` in
    the body has no per-email subject, and the request is about to 422 —
    but its per-IP rule still applies and still consumes, which is what
    keeps a flood of malformed bodies from being free.

    ## `principal` — why the authenticated identity is passed in

    A `USER`-scoped rule counts the account making the request, and this
    module cannot resolve that itself. Reading it from a header or a body
    would make the dimension spoofable, and importing `auth`'s
    `CurrentUser` dependency would make `app/api/` depend on a module's
    presentation layer, which dependency-injection.md §3.2 forbids — the
    whole reason this file sits under `app/api/` rather than `app/core/` is
    to keep that boundary legible.

    So the *caller* supplies it. A module's own `rate_limits.py` may import
    `CurrentUser` legitimately, resolves the principal there, and hands it
    to `RateLimit.enforce`. This function stays a pure function of a
    request plus what it was told.

    **A `USER` rule with no principal raises.** Unlike a missing email,
    this is never an ordinary request: a `USER`-scoped rule can only be
    attached to an authenticated route, whose auth dependency has already
    returned a principal or raised a 401. Reaching here without one means
    the guard was wired without one, and the alternative to raising is an
    endpoint that looks rate limited and is not (DI-06's argument, applied
    one layer later than startup because that is where the fact is
    knowable).
    """
    subjects: list[RateLimitSubject] = []
    email: str | None = None
    email_loaded = False

    for rule in rules:
        match rule.scope:
            case RateLimitScope.IP:
                subjects.append(
                    RateLimitSubject(
                        rule, client_ip(request, trusted_proxy_count=settings.trusted_proxy_count)
                    )
                )
            case RateLimitScope.EMAIL:
                # Read at most once per request even when several rules
                # want it — the body is cached, but the JSON parse is not.
                if not email_loaded:
                    email = await _email_from_body(request)
                    email_loaded = True
                if email is not None:
                    subjects.append(RateLimitSubject(rule, email))
            case RateLimitScope.USER:
                if principal is None:
                    raise RuntimeError(
                        f"rate limit rule {rule.name!r} is USER-scoped but no authenticated "
                        "principal was supplied; attach it with RateLimit.enforce(...)"
                    )
                subjects.append(RateLimitSubject(rule, principal))

    return subjects


def apply_headers(response: Response, decision: RateLimitDecision) -> None:
    """Publishes the binding rule's numbers on an allowed response.

    `X-RateLimit-Reset` is **delta-seconds**, not a Unix timestamp. Both
    conventions exist in the wild; delta-seconds is what
    draft-ietf-httpapi-ratelimit-headers specifies and it is the one that
    does not require the client's clock to agree with the server's — which
    on a mobile client is not a safe assumption.
    """
    response.headers[RATE_LIMIT_LIMIT_HEADER] = str(decision.rule.limit)
    response.headers[RATE_LIMIT_REMAINING_HEADER] = str(decision.remaining)
    response.headers[RATE_LIMIT_RESET_HEADER] = str(decision.retry_after_seconds)


class RateLimit:
    """A FastAPI dependency enforcing one endpoint's rules.

    Used as `dependencies=[Depends(RateLimit(...))]` on a route.

    ## Why it holds a resolver rather than the rules themselves

    A route's `dependencies=[...]` list is evaluated when its module is
    imported, so a guard constructed with concrete `RateLimitRule` objects
    would freeze whatever the environment looked like at import time. Two
    consequences, and the second is the one that matters:

      - a test that lowers a limit through `dependency_overrides` would
        have no effect, because the rules were built before the override
        existed. The suite would then only be able to test the *production*
        numbers, which means proving "the sixth login is refused" costs
        five real requests and proving an hour-long window resets is
        impossible;
      - `get_settings()` would be called during import of the router,
        which inverts the startup order `lifespan` establishes.

    Holding `rules_for` — a function from settings to rules — defers both.
    The settings arrive per request through the same injection every other
    dependency uses, so overriding them overrides the limits.

    ## Why it raises rather than returning a verdict

    A dependency that handed the endpoint a `RateLimitDecision` would leave
    every handler responsible for checking it, and the handler that forgets
    is the one that is unprotected while looking protected.
    """

    def __init__(
        self,
        endpoint: str,
        rules_for: Callable[[RateLimitSettings], Sequence[RateLimitRule]],
    ) -> None:
        self.endpoint = endpoint
        """Names the route this guards, for the block log and for the tests
        that assert every limited endpoint has a guard."""

        self._rules_for = rules_for

    def rules(self, settings: RateLimitSettings) -> Sequence[RateLimitRule]:
        """The rules this guard would apply under `settings`.

        Public so a test can assert *which* limits a route carries without
        sending eleven requests to infer it — the only other way to check
        that `POST /auth/login` counts per email as well as per IP.
        """
        rules = self._rules_for(settings)
        if not rules:
            raise ValueError(f"rate limit guard for {self.endpoint!r} resolved to no rules")
        return rules

    async def __call__(
        self,
        request: Request,
        response: Response,
        limiter: RateLimiterDep,
        settings: RateLimitSettingsDep,
    ) -> None:
        """The unauthenticated path — usable directly as `Depends(guard)`.

        Supplies no principal, so a guard whose rules include a
        `USER`-scoped one cannot be attached this way: `resolve_subjects`
        raises rather than silently counting nothing. An authenticated
        endpoint goes through `enforce` below.
        """
        await self.enforce(request, response, limiter=limiter, settings=settings)

    async def enforce(
        self,
        request: Request,
        response: Response,
        *,
        limiter: RateLimiter,
        settings: RateLimitSettings,
        principal: str | None = None,
    ) -> None:
        """The whole check, with the authenticated identity passed in.

        Split out of `__call__` by A64-012.5 so a module can supply a
        principal that this layer must not resolve for itself. A module's
        `rate_limits.py` wraps this in a dependency of its own that takes
        `CurrentUser`, which is an import that layer is allowed to make and
        this one is not — see `resolve_subjects` on why.

        Everything else is identical for both paths: the same all-or-
        nothing acquire, the same headers on success, the same WARNING on a
        block, the same `TooManyRequests`. There is deliberately no second
        copy of any of that.
        """
        subjects = await resolve_subjects(
            request, self.rules(settings), settings=settings, principal=principal
        )
        decision = await limiter.acquire(subjects)

        if decision.allowed:
            apply_headers(response, decision)
            return

        self._log_block(request, decision, settings=settings)
        raise TooManyRequests(
            retry_after=decision.retry_after_seconds,
            limit=decision.rule.limit,
            remaining=decision.remaining,
            reset_after=decision.retry_after_seconds,
        )

    @staticmethod
    def _log_block(
        request: Request, decision: RateLimitDecision, *, settings: RateLimitSettings
    ) -> None:
        """The abuse record — A64-011.8's logging requirement.

        WARNING, not INFO: services.md §7.1's level table puts "rate limit
        breached" at WARN, and it is right to. A single blocked request is
        an ordinary outcome, but the *rate* of these is the platform's only
        signal that a credential-stuffing run is in progress, and a signal
        buried at INFO alongside every successful request is not a signal.

        Carries the endpoint, the address and the rule; the timestamp is
        the log record's own (`app/common/logging.py` emits it on every
        line), so recording it again here would be a second, drifting
        answer to the same question.

        **The address is logged in full, while the same value is hashed
        before it becomes a Redis key**, and the asymmetry is deliberate
        rather than an inconsistency. Responding to abuse requires knowing
        which address to block, and the log pipeline has controlled access
        and a retention policy; a Redis keyspace has neither, and is
        readable in bulk by anything holding a connection.

        **The email is never logged**, only the name of the rule that
        fired. `login_email` tells an operator the dimension without
        putting an address in a log line (services.md §8.5) — and the
        blocked party is identified by IP, which is what a block list
        takes.
        """
        logger.warning(
            "rate_limit_blocked",
            extra={
                "endpoint": request.url.path,
                "method": request.method,
                "ip": client_ip(request, trusted_proxy_count=settings.trusted_proxy_count),
                "rule": decision.rule.name,
                "limit": decision.rule.limit,
                "retry_after": decision.retry_after_seconds,
            },
        )
