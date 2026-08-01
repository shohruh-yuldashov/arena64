"""`auth`'s rate-limit policy — which limits apply to which endpoint.

The mechanism is the platform's (`app/core/rate_limiting.py`,
`app/database/rate_limiter.py`, `app/api/rate_limiting.py`); the *policy*
is this module's, and it lives in `auth` because deciding that a login may
be attempted five times per quarter hour is a statement about
authentication, not about Redis. When `game` needs to throttle draw offers
(domain-model.md OF-2) it will write its own module-local policy against
the same three files and change none of them.

Every rule is built from `RateLimitSettings` rather than from literals, so
an incident is answered by an environment variable and a restart. The
numbers, and which of them A64-011.8 specified versus which were chosen,
are documented on that settings class rather than duplicated here — one
place for a figure, and it is the one an operator edits.

## Why login carries two rules and the others carry one

Per-IP and per-email answer different attacks, and neither is sufficient:

    per IP      bounds one host guessing many passwords for one account.
                Evaded by a botnet: a thousand hosts each get their own
                five attempts.
    per email   bounds the whole platform guessing at one account,
                however many hosts it comes from. Evaded by *credential
                stuffing*, which tries one password against a million
                accounts and never trips a per-account limit.

Login is where both attacks meet, so it carries both rules. The
all-or-nothing contract in `core.RateLimiter` is what makes running two
rules on one endpoint correct rather than merely doubled — see that
docstring on why a refused request must not consume the other bucket.

The remaining endpoints have one meaningful dimension each. Registration
has no account yet, so there is nothing but the host to count. The two
mail-senders are abusive against an *inbox*, so the address is the
dimension that protects the victim. Refresh presents an opaque token,
which is a credential rather than an identity — counting it would let an
attacker holding a thousand stolen tokens make a thousand times the
allowance, which counts the wrong noun.

## Why these are declared here and not on the routes

A route decorated with a literal `RateLimit(RateLimitRule(...), ...)`
would put the numbers in the router, where a reviewer reading the endpoint
sees a limit and cannot tell whether it matches the one two endpoints
below it. Naming them here gives one list to read, and gives
`tests/unit/test_auth_rate_limits.py` something to assert against without
sending eleven requests to infer it.
"""

from collections.abc import Sequence
from datetime import timedelta
from functools import lru_cache

from app.api.rate_limiting import RateLimit
from app.config.settings import RateLimitSettings
from app.core.rate_limiting import RateLimitRule, RateLimitScope


@lru_cache(maxsize=8)
def build_rules(settings: RateLimitSettings) -> dict[str, tuple[RateLimitRule, ...]]:
    """Every `auth` rule, keyed by the endpoint it guards.

    Built from settings rather than declared as module constants, so a
    limit changed in the environment — or overridden in a test — actually
    takes effect. See `RateLimit` on why constants would freeze the wrong
    moment.

    Cached on the settings object, which is safe because `RateLimitSettings`
    is frozen and therefore hashable: one process sees one or two distinct
    settings objects in its life, so this is a dict lookup per request
    rather than sixteen dataclass constructions. `maxsize=8` is sized for a
    test suite that varies configuration, not for production, where the
    answer is always one.
    """
    return {
        "login": (
            RateLimitRule(
                name="login_ip",
                scope=RateLimitScope.IP,
                limit=settings.login_ip_limit,
                window=timedelta(seconds=settings.login_ip_window_seconds),
            ),
            RateLimitRule(
                name="login_email",
                scope=RateLimitScope.EMAIL,
                limit=settings.login_email_limit,
                window=timedelta(seconds=settings.login_email_window_seconds),
            ),
        ),
        "register": (
            RateLimitRule(
                name="register_ip",
                scope=RateLimitScope.IP,
                limit=settings.register_ip_limit,
                window=timedelta(seconds=settings.register_ip_window_seconds),
            ),
        ),
        "forgot_password": (
            RateLimitRule(
                name="forgot_password_email",
                scope=RateLimitScope.EMAIL,
                limit=settings.forgot_password_email_limit,
                window=timedelta(seconds=settings.forgot_password_window_seconds),
            ),
        ),
        "reset_password": (
            RateLimitRule(
                name="reset_password_ip",
                scope=RateLimitScope.IP,
                limit=settings.password_reset_ip_limit,
                window=timedelta(seconds=settings.password_reset_window_seconds),
            ),
        ),
        "resend_verification": (
            RateLimitRule(
                name="resend_verification_email",
                scope=RateLimitScope.EMAIL,
                limit=settings.resend_verification_email_limit,
                window=timedelta(seconds=settings.resend_verification_window_seconds),
            ),
        ),
        "refresh": (
            RateLimitRule(
                name="refresh_ip",
                scope=RateLimitScope.IP,
                limit=settings.refresh_ip_limit,
                window=timedelta(seconds=settings.refresh_ip_window_seconds),
            ),
        ),
    }


def _guard(endpoint: str) -> RateLimit:
    """One endpoint's dependency.

    The guard captures a *lookup*, not the rules — the settings arrive per
    request. Constructing these at import time is therefore fine and is
    what lets the limits be declared on the route decorator where a reader
    of `router.py` can see that an endpoint is protected at all.

    The lookup raises `KeyError` at startup for an endpoint name that is
    not in `build_rules`, which is the failure worth having: a typo in a
    guard name would otherwise produce an endpoint with no limits and no
    error (DI-06 — fail before serving traffic, not on the ten-thousandth
    request).
    """

    def rules_for(settings: RateLimitSettings) -> Sequence[RateLimitRule]:
        return build_rules(settings)[endpoint]

    return RateLimit(endpoint, rules_for)


#: One guard per endpoint, named for the route it protects. Attached in
#: `router.py` as `dependencies=[Depends(LOGIN_RATE_LIMIT)]`.
LOGIN_RATE_LIMIT = _guard("login")
REGISTER_RATE_LIMIT = _guard("register")
FORGOT_PASSWORD_RATE_LIMIT = _guard("forgot_password")
RESET_PASSWORD_RATE_LIMIT = _guard("reset_password")
RESEND_VERIFICATION_RATE_LIMIT = _guard("resend_verification")
REFRESH_RATE_LIMIT = _guard("refresh")
