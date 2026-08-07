"""`notifications`' rate-limit policy — A64-021.3 §11.

The mechanism is the platform's (`app/core/rate_limiting.py`,
`app/database/rate_limiter.py`, `app/api/rate_limiting.py`); the *policy* is
this module's, exactly as `auth`'s, `profiles`', `friends`' and
`matchmaking`'s are theirs. Nothing shared changed to accommodate a fifth.

The numbers live on `RateLimitSettings` rather than here, for the reason
that class gives: a limit that can only be changed by a deploy cannot be
tightened during an incident.

## One rule, on the write

`PATCH /notifications/preferences` is the only endpoint here that is
limited. It is counted per **authenticated user**, which is the correct
dimension for a write behind a token and the one every settings endpoint on
this platform already uses — per-IP would make one office, one campus or one
mobile carrier a single bucket, so a handful of people changing their
settings would lock out everyone behind that address.

## Why the reads carry none

`GET /notifications/preferences` is unlimited, for the reason `GET
/profile/privacy` is: it reads at most a dozen of the caller's own rows over
their primary key, and a caller who repeats it a thousand times learns their
own settings a thousand times. There is nothing to accumulate and nothing to
enumerate — the endpoint has no parameter naming anybody else.

A64-021.1's four notification endpoints are likewise unlimited and stay
that way. This file exists for the preference write and does not
retroactively police them; if a list endpoint ever needs a bound, it gets
its own entry here with its own argument.
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
    """Every `notifications` rule, keyed by the endpoint group it guards.

    Built from settings rather than declared as module constants, so a limit
    changed in the environment — or overridden in a test — actually takes
    effect. Cached on the frozen settings object, as its four predecessors
    are.
    """
    return {
        "notification_preferences_update": (
            RateLimitRule(
                # Distinct from every other rule name on the platform, which
                # is what keeps this endpoint's traffic out of another
                # endpoint's bucket — see `RateLimitRule.name`.
                name="notification_preferences_update_user",
                scope=RateLimitScope.USER,
                limit=settings.notification_preferences_update_user_limit,
                window=timedelta(seconds=settings.notification_preferences_update_window_seconds),
            ),
        ),
        # A64-021.6. One rule shared by register and remove — see
        # `RateLimitSettings` on why the two halves of one action share a
        # bucket. `GET /push/status` carries none, like the preference read
        # it sits beside: it is one indexed read of the caller's own rows.
        "push_subscription": (
            RateLimitRule(
                name="push_subscription_user",
                scope=RateLimitScope.USER,
                limit=settings.push_subscription_user_limit,
                window=timedelta(seconds=settings.push_subscription_window_seconds),
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


PREFERENCES_UPDATE_RATE_LIMIT = _guard("notification_preferences_update")
PUSH_SUBSCRIPTION_RATE_LIMIT = _guard("push_subscription")


async def enforce_notification_preferences_update_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """The `PATCH /notifications/preferences` guard, counting per account.

    `user.id`, not the username: a handle can be changed and an id cannot,
    so counting the handle would make a rename a way to reset a limit.

    **The 401 comes first.** `CurrentUser` resolves before this body runs,
    so an unauthenticated request is refused without spending anybody's
    allowance — and without a principal there would be nothing to spend it
    against.
    """
    await PREFERENCES_UPDATE_RATE_LIMIT.enforce(
        request, response, limiter=limiter, settings=settings, principal=str(user.id)
    )


async def enforce_push_subscription_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """The push register/remove guard, counting per account — A64-021.6.

    `user.id` rather than the IP, for the reason `RateLimitSettings` gives:
    what this bounds is one account accumulating subscriptions, and an
    office behind one address is many accounts each entitled to their own
    devices.

    **The 401 comes first.** `CurrentUser` resolves before this body runs,
    so an unauthenticated request is refused without spending anybody's
    allowance.
    """
    await PUSH_SUBSCRIPTION_RATE_LIMIT.enforce(
        request, response, limiter=limiter, settings=settings, principal=str(user.id)
    )


__all__ = [
    "PREFERENCES_UPDATE_RATE_LIMIT",
    "PUSH_SUBSCRIPTION_RATE_LIMIT",
    "build_rules",
    "enforce_notification_preferences_update_limit",
    "enforce_push_subscription_limit",
]
