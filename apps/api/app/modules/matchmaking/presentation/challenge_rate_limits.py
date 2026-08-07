"""`matchmaking`'s challenge limits — A64-022.2 §18.

The mechanism is the platform's (`app/core/rate_limiting.py`,
`app/database/rate_limiter.py`, `app/api/rate_limiting.py`); the *policy* is
this module's, and it lives here because deciding that twenty invitations an
hour is enough is a statement about challenges rather than about Redis.

Its own file rather than `rate_limits.py`, which holds the queue's: that one
is built around `QueueType` and `Region` and is keyed by pool, and a second
aggregate's rules in it would make every rule there something a reader has to
classify.

## Both rules count the **account**, never the address

What they bound is one person's behaviour, and a per-IP budget is defeated by
a botnet while throttling a shared connection for everybody behind it — the
argument `friends` already made for the same two shapes of action.

The numbers match `friends`' deliberately. Sending a challenge and sending a
friend request are the same act with a different payload, and two figures for
one shape would be two things to explain during an incident rather than one
to tune.

## Environment scaling is automatic

A64-021.7 made every limit environment-aware at the guard, so these are
production's figures and a laptop gets twenty times them without anything
here saying so.
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
    """Every challenge rule, keyed by the endpoint group it guards.

    Built from settings rather than declared as constants, so a limit changed
    in the environment — or overridden in a test — actually takes effect.
    Cached on the frozen settings object, as its predecessors are.
    """
    return {
        "challenge_create": (
            RateLimitRule(
                # Distinct from every other rule name on the platform, which
                # is what keeps this endpoint's traffic out of another's
                # bucket — see `RateLimitRule.name`.
                name="challenge_create_user",
                scope=RateLimitScope.USER,
                limit=settings.challenge_create_user_limit,
                window=timedelta(seconds=settings.challenge_create_window_seconds),
            ),
        ),
        "challenge_respond": (
            RateLimitRule(
                name="challenge_respond_user",
                scope=RateLimitScope.USER,
                limit=settings.challenge_respond_user_limit,
                window=timedelta(seconds=settings.challenge_respond_window_seconds),
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


CHALLENGE_CREATE_RATE_LIMIT = _guard("challenge_create")
CHALLENGE_RESPOND_RATE_LIMIT = _guard("challenge_respond")


async def enforce_challenge_create_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """`POST /challenges`, counted per account.

    `user.id`, not the username: a handle can be changed and an id cannot, so
    counting the handle would make a rename a way to reset a limit.

    **The 401 comes first.** `CurrentUser` resolves before this body runs, so
    an unauthenticated request is refused without spending anybody's
    allowance — and without a principal there would be nothing to spend it
    against.
    """
    await CHALLENGE_CREATE_RATE_LIMIT.enforce(
        request, response, limiter=limiter, settings=settings, principal=str(user.id)
    )


async def enforce_challenge_respond_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """Decline and cancel, sharing one counter — see the module docstring."""
    await CHALLENGE_RESPOND_RATE_LIMIT.enforce(
        request, response, limiter=limiter, settings=settings, principal=str(user.id)
    )


__all__ = [
    "CHALLENGE_CREATE_RATE_LIMIT",
    "CHALLENGE_RESPOND_RATE_LIMIT",
    "build_rules",
    "enforce_challenge_create_limit",
    "enforce_challenge_respond_limit",
]
