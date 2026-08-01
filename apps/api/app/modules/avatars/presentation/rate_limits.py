"""`avatars`' rate-limit policy — A64-013.2.

The mechanism is the platform's; the *policy* is this module's, exactly as
`auth`'s, `profiles`' and `friends`' are theirs. One rule, on one endpoint.

## Why the upload and not the read or the delete

`POST /profile/avatar` is the most expensive operation on the platform per
call: it decodes an image, applies orientation, and encodes twice — on bytes
a caller supplies. That makes it the cheapest CPU-amplification primitive
here, and the fact that it is authenticated bounds it only to the number of
accounts an attacker holds, which is one registration each.

`GET` and `DELETE` are an indexed row read and a three-column update. Neither
amplifies anything, and limiting them would spend a budget a settings screen
needs on page load — the same argument `profiles` makes for leaving
`GET /profile/privacy` unlimited.

## Per user

The endpoint is authenticated and addresses no account but the caller's, so
the platform knows exactly whose budget to spend. Per-IP would throttle a
shared network for one person's behaviour while doing nothing about an
attacker with several accounts.
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
    """Every `avatars` rule, keyed by the endpoint it guards.

    Built from settings rather than declared as module constants, so a limit
    changed in the environment — or overridden in a test — actually takes
    effect. Cached on the frozen settings object, as every other module's
    equivalent is.
    """
    return {
        "avatar_upload": (
            RateLimitRule(
                name="avatar_upload_user",
                scope=RateLimitScope.USER,
                limit=settings.avatar_upload_user_limit,
                window=timedelta(seconds=settings.avatar_upload_window_seconds),
            ),
        ),
    }


def _rules_for(settings: RateLimitSettings) -> Sequence[RateLimitRule]:
    return build_rules(settings)["avatar_upload"]


AVATAR_UPLOAD_RATE_LIMIT = RateLimit("avatar_upload", _rules_for)


async def enforce_avatar_upload_limit(
    request: Request,
    response: Response,
    user: CurrentUser,
    limiter: RateLimiterDep,
    settings: RateLimitSettingsDep,
) -> None:
    """The `POST /profile/avatar` guard, counting per account.

    A wrapper rather than a bare `Depends(AVATAR_UPLOAD_RATE_LIMIT)`, and the
    indirection is the mechanism rather than ceremony: a `USER`-scoped rule
    needs the authenticated principal, and `app/api/rate_limiting.py` must
    not resolve one — reading an identity from a header would make the
    dimension spoofable, and importing `auth`'s `CurrentUser` there would
    make `app/api/` depend on a module's presentation layer
    (dependency-injection.md §3.2). **This** file is a module presentation
    layer, so it may.

    `CurrentUser` resolving first is also what keeps an unauthenticated
    upload from reaching Pillow: the `401` is decided before a single byte
    is decoded.
    """
    await AVATAR_UPLOAD_RATE_LIMIT.enforce(
        request, response, limiter=limiter, settings=settings, principal=str(user.id)
    )
