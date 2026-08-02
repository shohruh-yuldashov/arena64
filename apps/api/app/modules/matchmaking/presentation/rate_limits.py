"""`matchmaking`'s rate-limit policy — A64-014.1.

The mechanism is the platform's (`app/core/rate_limiting.py`,
`app/database/rate_limiter.py`, `app/api/rate_limiting.py`); the *policy* is
this module's, exactly as `auth`'s, `profiles`' and `friends`' are theirs.
Nothing in the shared mechanism changed to accommodate a fourth module.

The numbers live on `RateLimitSettings` rather than here, for the reason
that class gives: a limit that can only be changed by a deploy cannot be
tightened during an incident.

## One budget across join and leave

Both endpoints share `matchmaking_queue`, and the reason is that the abuse
they enable is one behaviour rather than two. Nobody joins a queue two
hundred times; they join and leave two hundred times, and a client stuck in
that loop — or a player trying to re-roll their pool — spends both budgets
in lockstep. Two counters would be two numbers to tune and neither could be
exhausted without the other.

`GET /matchmaking/queue/me` carries no limit. It is a read of the caller's
own row, it is the endpoint a client polls while waiting, and throttling it
would make the queue *look* broken in exactly the situation it is working —
a player watching a spinner. What it costs is bounded by two indexed reads
against a partial index, which is the cheapest authenticated read on the
platform. `GET /matchmaking/matches/pending` carries none for the same
reason and is even cheaper: one indexed read.

## Acceptance has its own budget, and does not share the queue's

A64-015.4 adds a second group, and the decision worth recording is that it
is *separate* rather than a third endpoint on `matchmaking_queue`. The
argument for sharing there was that joining and leaving are one behaviour;
accepting is not that behaviour. A player who has spent their queue budget
churning pools must still be able to answer the match the platform has
already paired them into — a shared counter would mean the queue creating a
match and then refusing to let one of its two players say yes, which turns
a rate limit into a stuck game for somebody else.

## Per authenticated user, not per IP

Every endpoint here sits behind a token, so the platform knows whose account
is acting. The dimension matters more than on a settings screen: what this
bounds is *pool churn* — a player repeatedly joining and leaving to influence
who they are paired with, which is a rating-manipulation vector
(domain-model.md QT-3 names the adjacent one) — and only a per-account
budget counts that. A per-IP limit would let it be spread across hosts and
would throttle a university network for one student.
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
    """Every `matchmaking` rule, keyed by the endpoint group it guards.

    Built from settings rather than declared as module constants, so a limit
    changed in the environment — or overridden in a test — actually takes
    effect. Cached on the frozen settings object, as the other three
    modules' equivalents are.
    """
    return {
        "matchmaking_queue": (
            RateLimitRule(
                # Distinct from every other rule name on the platform, which
                # is what keeps this endpoint's traffic out of another
                # endpoint's bucket — see `RateLimitRule.name`.
                name="matchmaking_queue_user",
                scope=RateLimitScope.USER,
                limit=settings.matchmaking_queue_user_limit,
                window=timedelta(seconds=settings.matchmaking_queue_window_seconds),
            ),
        ),
        "matchmaking_acceptance": (
            RateLimitRule(
                name="matchmaking_acceptance_user",
                scope=RateLimitScope.USER,
                limit=settings.matchmaking_acceptance_user_limit,
                window=timedelta(seconds=settings.matchmaking_acceptance_window_seconds),
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


QUEUE_RATE_LIMIT = _guard("matchmaking_queue")
ACCEPTANCE_RATE_LIMIT = _guard("matchmaking_acceptance")


async def enforce_queue_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """The shared guard for joining and leaving a queue.

    `user.id`, not the username: a handle can be changed (UP-2, once that
    exists) and an id cannot, so counting the handle would make a rename a
    way to reset a limit.

    **The 401 comes first.** `CurrentUser` resolves before this body runs,
    so an unauthenticated request is refused without spending anybody's
    allowance — and without a principal there would be nothing to spend it
    against.
    """
    await QUEUE_RATE_LIMIT.enforce(
        request, response, limiter=limiter, settings=settings, principal=str(user.id)
    )


async def enforce_acceptance_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """The shared guard for accepting and declining a match — A64-015.4.

    One budget across both answers, exactly as joining and leaving share
    one: a client stuck in a retry loop spends them in lockstep, and two
    counters would be two numbers to tune and neither could be exhausted
    without the other. Unlike that pair, this one does **not** share with
    the queue — see this module's docstring.

    `user.id`, not the username, and the 401 comes first: `CurrentUser`
    resolves before this body runs, so an unauthenticated request is
    refused without spending anybody's allowance.
    """
    await ACCEPTANCE_RATE_LIMIT.enforce(
        request, response, limiter=limiter, settings=settings, principal=str(user.id)
    )
