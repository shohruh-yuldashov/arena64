"""`friends`' rate-limit policy — A64-013.2.

The mechanism is the platform's (`app/core/rate_limiting.py`,
`app/database/rate_limiter.py`, `app/api/rate_limiting.py`); the *policy* is
this module's, exactly as `auth`'s and `profiles`' are theirs. Nothing in the
shared mechanism changed to accommodate a third module, which is the property
`auth.presentation.rate_limits` predicted and `profiles` first demonstrated.

The numbers live on `RateLimitSettings` rather than here, for the reason that
class gives: a limit that can only be changed by a deploy cannot be tightened
during an incident.

## All four writes are counted per authenticated user

Every friend-request endpoint sits behind a token, so the platform knows
whose account is acting — a far better subject than a network address shared
by everyone behind one NAT.

The dimension matters more here than on a settings screen. The abuse this
bounds is **harassment**, not load: FR-1 already stops a second pending
request to the same person, so an attacker's remaining move is to spray
requests at *many* people, and only a per-account budget counts that. A
per-IP limit would let a botnet spread it and would throttle a university
network for one student's behaviour.

## Two budgets, not four

Sending has its own limit; the three resolutions share one.

Sending is the endpoint with a victim — every call puts a notification in
somebody else's list — so it gets the tighter budget. Accepting, declining
and cancelling can only touch requests that already exist and that the
caller is party to, so their bound exists to stop a stuck client hammering
a row, not to protect anybody. Giving each its own counter would be three
budgets nobody can exhaust separately and three numbers to tune.
"""

from collections.abc import Sequence
from datetime import timedelta
from functools import lru_cache

from fastapi import Request, Response

from app.api.deps import RateLimiterDep, RateLimitSettingsDep
from app.api.rate_limiting import RateLimit
from app.config.settings import RateLimitSettings
from app.core.rate_limiting import RateLimitRule, RateLimitScope
from app.modules.auth.presentation.dependencies import CurrentUser


@lru_cache(maxsize=8)
def build_rules(settings: RateLimitSettings) -> dict[str, tuple[RateLimitRule, ...]]:
    """Every `friends` rule, keyed by the endpoint group it guards.

    Built from settings rather than declared as module constants, so a limit
    changed in the environment — or overridden in a test — actually takes
    effect. Cached on the frozen settings object, as `auth`'s and `profiles`'
    equivalents are.
    """
    return {
        "friend_request_send": (
            RateLimitRule(
                # Distinct from every other rule name on the platform, which
                # is what keeps this endpoint's traffic out of another
                # endpoint's bucket — see `RateLimitRule.name`.
                name="friend_request_send_user",
                scope=RateLimitScope.USER,
                limit=settings.friend_request_send_user_limit,
                window=timedelta(seconds=settings.friend_request_send_window_seconds),
            ),
        ),
        "friend_request_respond": (
            RateLimitRule(
                name="friend_request_respond_user",
                scope=RateLimitScope.USER,
                limit=settings.friend_request_respond_user_limit,
                window=timedelta(seconds=settings.friend_request_respond_window_seconds),
            ),
        ),
    }


def _guard(endpoint: str) -> RateLimit:
    """One endpoint group's dependency.

    The guard captures a *lookup*, not the rules — the settings arrive per
    request. The lookup raises `KeyError` at startup for a name that is not
    in `build_rules`, which is the failure worth having: a typo would
    otherwise produce an endpoint with no limits and no error (DI-06).
    """

    def rules_for(settings: RateLimitSettings) -> Sequence[RateLimitRule]:
        return build_rules(settings)[endpoint]

    return RateLimit(endpoint, rules_for)


SEND_RATE_LIMIT = _guard("friend_request_send")
RESPOND_RATE_LIMIT = _guard("friend_request_respond")


async def _enforce(
    guard: RateLimit,
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """Resolve the authenticated principal and spend one unit against
    `guard`.

    The shared body of the two dependencies below — the same shape
    `profiles.presentation.rate_limits` uses, and the same reason two thin
    wrappers exist rather than one parametrised callable: FastAPI identifies
    a route dependency by the function object, so a single shared one would
    make "is this route limited, and by which policy" unanswerable from the
    decorator.

    `user.id`, not the username: a handle can be changed (UP-2, once that
    exists) and an id cannot, so counting the handle would make a rename a
    way to reset a limit.

    **The 401 comes first.** `CurrentUser` resolves before this body runs,
    so an unauthenticated request is refused without spending anybody's
    allowance — and without a principal there would be nothing to spend it
    against.
    """
    await guard.enforce(
        request, response, limiter=limiter, settings=settings, principal=str(user.id)
    )


async def enforce_friend_request_send_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """The `POST /friends/requests` guard, counting per account.

    The tighter of the two budgets, because this is the only friend-request
    endpoint with a victim: every successful call puts a row in somebody
    else's incoming list.
    """
    await _enforce(SEND_RATE_LIMIT, request, response, user, limiter, settings)


async def enforce_friend_request_respond_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """The shared guard for accept, decline and cancel.

    One budget across the three, because none of them can reach a request
    the caller is not party to — the bound exists to stop a stuck client
    hammering a row, not to protect a third party.
    """
    await _enforce(RESPOND_RATE_LIMIT, request, response, user, limiter, settings)
