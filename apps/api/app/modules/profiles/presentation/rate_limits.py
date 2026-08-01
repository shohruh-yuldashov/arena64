"""`profiles`' rate-limit policy — A64-012.4.

The mechanism is the platform's (`app/core/rate_limiting.py`,
`app/database/rate_limiter.py`, `app/api/rate_limiting.py`); the *policy* is
this module's, exactly as `auth.presentation.rate_limits` is `auth`'s. That
module's docstring predicted this file — "when `game` needs to throttle draw
offers it will write its own module-local policy against the same three
files and change none of them" — and this is the first time it happened.
Nothing in the shared mechanism changed to accommodate it.

The numbers live on `RateLimitSettings` rather than here, for the reason
that class gives: a limit that can only be changed by a deploy cannot be
tightened during an incident.

## Both writes are counted per authenticated user

`PATCH /profile/preferences` was the first USER-scoped limit on the
platform (A64-012.5, which added the scope); `PATCH /profile/privacy`
joined it in A64-012.6.

The dimension is the point. Both endpoints sit behind a token, so the
platform knows exactly whose account is being written — a far better
subject than a network address shared by everyone behind one NAT. Per-IP on
a settings endpoint has the failure mode `RateLimitSettings.trusted_proxy_count`
describes: an office, a university or a mobile carrier is one bucket, so
twenty changes across all of its users exhausts it for everyone.

Neither carries a per-IP companion rule. Adding one would reintroduce
exactly the shared-NAT problem the user scope removes, and there is no
attack it would catch that the per-user rule does not — an attacker holding
N stolen tokens already holds N compromised accounts, and rate limiting is
not the control for that.

## Why the guard is on the write and not the read

`GET /profile/privacy` is unlimited. It is a single indexed read of a row
the caller already authenticated as, it changes nothing, and its output is
five booleans the caller already owns — there is no amplification and no
victim. A64-012.4 asks for the limit on `PATCH` specifically, and adding
one to the read would spend an allowance a settings screen needs on page
load.
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
    """Every `profiles` rule, keyed by the endpoint it guards.

    Built from settings rather than declared as module constants, so a
    limit changed in the environment — or overridden in a test — actually
    takes effect. Cached on the frozen settings object, as `auth`'s
    equivalent is; see that module for why both are necessary.
    """
    return {
        "privacy_update": (
            RateLimitRule(
                # Distinct from every `auth` rule name, which is what keeps
                # this endpoint's traffic out of another endpoint's bucket
                # — see `RateLimitRule.name`.
                #
                # `_user`, not `_ip`, since A64-012.6 migrated it. The
                # rename matters as much as the scope change: the old
                # buckets were keyed on hashed addresses and the new ones
                # on account ids, so sharing a name would have made every
                # live IP bucket look like a user bucket until it expired.
                name="privacy_update_user",
                scope=RateLimitScope.USER,
                limit=settings.privacy_update_user_limit,
                window=timedelta(seconds=settings.privacy_update_window_seconds),
            ),
        ),
        "preferences_update": (
            RateLimitRule(
                name="preferences_update_user",
                scope=RateLimitScope.USER,
                limit=settings.preferences_update_user_limit,
                window=timedelta(seconds=settings.preferences_update_window_seconds),
            ),
        ),
    }


def _guard(endpoint: str) -> RateLimit:
    """One endpoint's dependency.

    The guard captures a *lookup*, not the rules — the settings arrive per
    request. The lookup raises `KeyError` at startup for a name that is not
    in `build_rules`, which is the failure worth having: a typo would
    otherwise produce an endpoint with no limits and no error (DI-06).
    """

    def rules_for(settings: RateLimitSettings) -> Sequence[RateLimitRule]:
        return build_rules(settings)[endpoint]

    return RateLimit(endpoint, rules_for)


#: The two guards. Both are USER-scoped, so neither is attachable as a bare
#: `Depends(...)` — each has a wrapper below that resolves the principal.
PRIVACY_UPDATE_RATE_LIMIT = _guard("privacy_update")
PREFERENCES_UPDATE_RATE_LIMIT = _guard("preferences_update")


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

    The shared body of the two dependencies below, which differ only in
    which guard they carry. Two thin wrappers rather than one parametrised
    dependency because FastAPI identifies a route dependency by the
    function object — a single shared callable would make
    "is this route rate limited, and by which policy" unanswerable from the
    route decorator, and untestable without sending requests.

    `user.id`, not the username: a handle can be changed (UP-2, once that
    exists) and an id cannot, so counting the handle would make a rename a
    way to reset a limit.

    **The 401 comes first**, which is the ordering to want. `CurrentUser`
    resolves before this body runs, so an unauthenticated request is refused
    without spending anybody's allowance — and, more to the point, without a
    principal there would be nothing to spend it against.
    """
    await guard.enforce(
        request,
        response,
        limiter=limiter,
        settings=settings,
        principal=str(user.id),
    )


async def enforce_privacy_update_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """The `PATCH /profile/privacy` guard, counting per account.

    Added by A64-012.6, replacing the bare
    `Depends(PRIVACY_UPDATE_RATE_LIMIT)` A64-012.4 shipped. That form still
    works for an IP-scoped guard and cannot work for a USER-scoped one —
    `resolve_subjects` raises without a principal rather than silently
    counting nothing, so the migration could not half-happen.
    """
    await _enforce(PRIVACY_UPDATE_RATE_LIMIT, request, response, user, limiter, settings)


async def enforce_preferences_update_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """The `PATCH /profile/preferences` guard, counting per account.

    A dependency of this module's own rather than a bare
    `Depends(PREFERENCES_UPDATE_RATE_LIMIT)`, and the indirection is the
    whole mechanism rather than ceremony.

    A `USER`-scoped rule needs the authenticated principal, and
    `app/api/rate_limiting.py` must not resolve one: reading an identity
    from a header would make the dimension spoofable, and importing
    `auth`'s `CurrentUser` there would make `app/api/` depend on a module's
    presentation layer (dependency-injection.md §3.2). **This** file is a
    module presentation layer, so it may import `CurrentUser` — and that is
    the only reason these two wrappers exist.
    """
    await _enforce(PREFERENCES_UPDATE_RATE_LIMIT, request, response, user, limiter, settings)
