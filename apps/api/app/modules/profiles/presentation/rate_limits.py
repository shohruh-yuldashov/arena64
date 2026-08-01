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

## Why per IP on an authenticated endpoint, and what that costs

`RateLimitScope` offers two dimensions: the caller's network address, and
an email address in the request body. `PATCH /profile/privacy` carries no
email, so per-IP is the only rule this endpoint can currently express.

**Per user would be the better dimension**, and the gap is worth recording
rather than glossing. The caller here is *authenticated* — the platform
knows exactly whose account is being written, which is a far better subject
than a network address shared by everyone behind one NAT. Per-IP on this
endpoint has the failure mode `RateLimitSettings.trusted_proxy_count`
describes: an office, a university or a mobile carrier NAT is one bucket, so
twenty settings changes across all of its users exhausts it for everyone.
The limit here is set generously for that reason, which is a mitigation
rather than a fix.

Adding a `RateLimitScope.USER` is not a line of code — it is a design
decision about where an authenticated principal reaches the rate-limit
dependency. `app/api/rate_limiting.py` deliberately imports no module
(dependency-injection.md §3.2 keeps `app/api/` free of module presentation
layers), so it cannot depend on `auth`'s `CurrentUser`; making it do so, or
threading the principal through `request.state` and relying on dependency
ordering, are both real changes to shared machinery with security
consequences for six existing endpoints. That belongs in its own task with
its own tests, not in a privacy feature. It is A64-012.5's first
recommendation.

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

from app.api.rate_limiting import RateLimit
from app.config.settings import RateLimitSettings
from app.core.rate_limiting import RateLimitRule, RateLimitScope


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
                name="privacy_update_ip",
                scope=RateLimitScope.IP,
                limit=settings.privacy_update_ip_limit,
                window=timedelta(seconds=settings.privacy_update_window_seconds),
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


#: Attached in `self_router.py` as `dependencies=[Depends(...)]`.
PRIVACY_UPDATE_RATE_LIMIT = _guard("privacy_update")
