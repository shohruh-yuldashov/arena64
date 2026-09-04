"""The collector's rate-limit policy — analytics.md §37, §40.

The endpoint accepts input from any browser, authenticated or not, so it
needs a bound that does not depend on there being an account. Two rules, and
the split is the one `profiles` and `tournament` already make:

    per account   the tighter and more meaningful bound. An account is a
                  subject the platform can count *and revoke*
    per address   the only dimension left for an anonymous caller, and a
                  weaker one: an office or a mobile carrier is one bucket,
                  so the budget is looser by design

Both are generous relative to what the client sends. The tracker batches and
flushes on a timer and on page hide, so a busy session produces a handful of
requests a minute; a caller near either limit is not a person browsing.

## IP is used here and stored nowhere

The rate limiter reads the address to count against it. The analytics event
does **not** carry it, is not derived from it, and cannot be — §12 of the
document, and the distinction is worth stating because "we rate-limit by IP"
and "we store IPs" are routinely conflated. Transient use by security
infrastructure is not collection.
"""

from collections.abc import Sequence
from datetime import timedelta

from fastapi import Request, Response

from app.api.deps import RateLimiterDep, RateLimitSettingsDep
from app.api.rate_limiting import RateLimit
from app.config.settings import RateLimitSettings
from app.core.rate_limiting import RateLimitRule, RateLimitScope
from app.modules.auth.presentation.dependencies import OptionalCurrentUser


def build_rules(settings: RateLimitSettings) -> dict[str, Sequence[RateLimitRule]]:
    """This module's policy, keyed by endpoint.

    **Two entries for one route**, and the split is forced by the limiter
    rather than chosen: a `USER`-scoped rule raises when no principal was
    supplied, and it is right to — a limiter that silently skipped a scope
    it could not resolve would stop counting the moment authentication
    changed shape. So an anonymous caller is measured by one rule and a
    signed-in one by two, and the dependency below picks between them.
    """
    ip_rule = RateLimitRule(
        name="analytics_collect_ip",
        scope=RateLimitScope.IP,
        limit=settings.analytics_collect_ip_limit,
        window=timedelta(seconds=settings.analytics_collect_window_seconds),
    )
    return {
        "analytics_collect": (ip_rule,),
        "analytics_collect_authenticated": (
            ip_rule,
            RateLimitRule(
                name="analytics_collect_user",
                scope=RateLimitScope.USER,
                limit=settings.analytics_collect_user_limit,
                window=timedelta(seconds=settings.analytics_collect_window_seconds),
            ),
        ),
    }


def _guard(endpoint: str) -> RateLimit:
    def rules_for(settings: RateLimitSettings) -> Sequence[RateLimitRule]:
        return build_rules(settings)[endpoint]

    return RateLimit(endpoint, rules_for)


_ANONYMOUS_GUARD = _guard("analytics_collect")
_AUTHENTICATED_GUARD = _guard("analytics_collect_authenticated")


async def enforce_analytics_collect_limit(
    request: Request,
    response: Response,
    viewer: OptionalCurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """Spend one unit, against an account when there is one.

    **A dependency rather than a bare `Depends(guard)`, and that is forced
    rather than chosen.** A `USER`-scoped rule attached bare raises when no
    principal was supplied — correctly, because a limiter that silently
    skipped a scope it could not resolve would be a limiter that stops
    counting the moment authentication changes shape.

    So the principal is resolved here, where `OptionalCurrentUser` is an
    import this layer is allowed to make, and it selects the rule set. An
    anonymous caller spends against the address rule alone; a signed-in one
    spends against both, and the account bound is the meaningful one because
    an account is revocable and an address is shared by an office.
    """
    if viewer is None:
        await _ANONYMOUS_GUARD.enforce(request, response, limiter=limiter, settings=settings)
        return

    await _AUTHENTICATED_GUARD.enforce(
        request, response, limiter=limiter, settings=settings, principal=str(viewer.id)
    )
