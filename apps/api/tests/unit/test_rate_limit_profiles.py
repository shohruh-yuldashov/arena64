"""Environment-scaled rate limits — A64-021.6 §9.

The limiter is production's, always. What changes between a deployed tier
and a laptop is a **multiplier**, and this file is the proof that it changes
nothing else.

## Why this is worth testing at all

The mechanism is small and its failure mode is the worst one available: a
bug here does not break a test, it *weakens production security silently*.
Nothing about a hundred-fold `login_ip` limit looks wrong in a log, a
dashboard or a response — it looks like nobody is attacking.

So the assertions below are mostly about what must **not** happen:

    production is untouched      the declared figures are what a deployed
                                 tier enforces, byte for byte
    the environment decides      `RATE_LIMIT_PROFILE` in the process
                                 environment is ignored
    an unknown tier is strict    anything unrecognised gets production
    the limiter still limits     scaling raises the ceiling; it does not
                                 remove one

No Redis and no HTTP: these are the pure functions and the settings
resolution. `tests/contract/test_rate_limiter.py` and
`tests/contract/test_rate_limiting_api.py` cover enforcement, and both pass
unchanged: the mechanism they exercise is the one this scales.
"""

from datetime import timedelta

import pytest

from app.config.environment import Environment
from app.config.settings import RateLimitSettings, rate_limit_profile_for
from app.core.rate_limiting import (
    RateLimitProfile,
    RateLimitRule,
    RateLimitScope,
    scaled,
    scaled_all,
)
from app.modules.auth.presentation.rate_limits import build_rules as auth_rules

LOGIN_IP = RateLimitRule(
    name="login_ip", scope=RateLimitScope.IP, limit=20, window=timedelta(minutes=15)
)


class TestProductionIsUnchanged:
    def test_the_production_profile_returns_the_rule_itself(self) -> None:
        """Identity, not equality — the strongest form of "unchanged".

        A deployed tier gets the object the module policy declared, so there
        is no arithmetic on the production path at all and nothing that
        could round, overflow or be applied twice.
        """
        assert scaled(LOGIN_IP, RateLimitProfile.PRODUCTION) is LOGIN_IP

    def test_every_declared_auth_limit_survives_production_untouched(self) -> None:
        """The real policy registry, not a fixture.

        `tests/unit/test_auth_rate_limits.py` asserts the declared figures
        against a written-out table; this asserts that scaling under
        `PRODUCTION` does not disturb any of them. Together they are "the
        numbers are these, and production still gets exactly these".
        """
        declared = [rule for rules in auth_rules(RateLimitSettings()).values() for rule in rules]

        scaled_rules = scaled_all(declared, RateLimitProfile.PRODUCTION)

        assert list(scaled_rules) == declared

    def test_staging_is_as_strict_as_production(self) -> None:
        """Staging is a deployed tier reachable from the internet. It gets
        production's limits, and this is asserted rather than left to be
        inferred from the mapping's silence."""
        assert rate_limit_profile_for(Environment.STAGING) is RateLimitProfile.PRODUCTION
        assert rate_limit_profile_for(Environment.PRODUCTION) is RateLimitProfile.PRODUCTION


class TestRelaxedEnvironments:
    def test_development_is_higher_than_production(self) -> None:
        """A working afternoon on the sign-in screen must not lock a
        developer out of their own laptop."""
        assert rate_limit_profile_for(Environment.LOCAL) is RateLimitProfile.DEVELOPMENT

        relaxed = scaled(LOGIN_IP, RateLimitProfile.DEVELOPMENT)

        assert relaxed.limit > LOGIN_IP.limit
        assert relaxed.limit == 20 * 20

    def test_test_and_ci_are_higher_still(self) -> None:
        """The end-to-end suite's whole traffic comes from one address, and
        it must be runnable repeatedly without somebody first learning to
        clear buckets."""
        assert rate_limit_profile_for(Environment.TEST) is RateLimitProfile.TEST
        assert rate_limit_profile_for(Environment.CI) is RateLimitProfile.TEST

        relaxed = scaled(LOGIN_IP, RateLimitProfile.TEST)

        assert relaxed.limit == 20 * 100
        assert relaxed.limit > scaled(LOGIN_IP, RateLimitProfile.DEVELOPMENT).limit

    @pytest.mark.parametrize("profile", list(RateLimitProfile))
    def test_scaling_never_touches_the_window_the_scope_or_the_name(
        self, profile: RateLimitProfile
    ) -> None:
        """The **shape** of every limit is preserved.

        Widening the window too would change what a limit means — "20 in 15
        minutes" and "20 in 5 hours" are different rules — where multiplying
        the count keeps the rule and only adds headroom.

        The name matters for a second reason: it is the Redis bucket
        namespace, so a scaled rule that renamed itself would count into a
        different bucket from the one `app.operator.rate_limits clear`
        knows about.
        """
        relaxed = scaled(LOGIN_IP, profile)

        assert relaxed.name == LOGIN_IP.name
        assert relaxed.scope is LOGIN_IP.scope
        assert relaxed.window == LOGIN_IP.window


class TestItCannotSilentlyWeakenProduction:
    def test_an_unrecognised_environment_gets_production_limits(self) -> None:
        """**Fail-closed**, and this is the assertion the whole mechanism
        rests on.

        A tier nobody thought about must be strict, because the alternative
        is a new environment silently shipping with hundred-fold allowances
        and nothing about it looking wrong. `Environment` is a closed enum,
        so this is reachable only by adding a member — which is exactly the
        change that would otherwise introduce the hole.
        """
        unknown = "a-tier-invented-next-year"

        assert rate_limit_profile_for(unknown) is RateLimitProfile.PRODUCTION  # type: ignore[arg-type]

    def test_the_environment_variable_cannot_choose_the_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`RATE_LIMIT_PROFILE` is ignored — §10, "environment configuration
        is the authority".

        An operator-settable profile would be a way to ship hundred-fold
        limits to production by editing one variable, which is the exact
        failure this mechanism must not introduce. `get_settings` overwrites
        whatever the environment says, and this proves the override rather
        than trusting the docstring.
        """
        from app.config.settings import get_settings

        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("RATE_LIMIT_PROFILE", "test")
        # Everything a production-like tier refuses to start without.
        monkeypatch.setenv("POSTGRES_DSN", "postgresql+asyncpg://a:b@db:5432/arena64")
        for role in ("LIVE", "BUS", "BROKER", "CACHE", "LIMITS"):
            monkeypatch.setenv(f"REDIS_{role}_URL", f"redis://redis:6379/{role.lower()}")
        monkeypatch.setenv("JWT_SECRET_KEY", "x" * 64)
        monkeypatch.setenv("EMAIL_VERIFICATION_OTP_SECRET", "y" * 64)
        monkeypatch.setenv("PUBLIC_APP_URL", "https://arena64.gg")
        monkeypatch.setenv("BROWSER_SESSION_TRUSTED_ORIGINS", '["https://arena64.gg"]')
        # A64-028.6: a deployed tier must say how the operator surface is
        # guarded before it will start at all.
        monkeypatch.setenv("OPS_TOKEN", "ops-token")
        get_settings.cache_clear()

        try:
            settings = get_settings()
            assert settings.rate_limit.profile is RateLimitProfile.PRODUCTION
        finally:
            get_settings.cache_clear()

    def test_a_hand_built_settings_object_is_strict(self) -> None:
        """The default is `PRODUCTION`, so a `RateLimitSettings` constructed
        anywhere — in a test, by a future caller, by a script — is the
        strict one until something deliberately relaxes it."""
        assert RateLimitSettings().profile is RateLimitProfile.PRODUCTION


class TestTheGuardAppliesIt:
    """The scaling has to reach the thing that enforces it.

    Every assertion above is about pure functions; these are about the
    **guard**, which is the single funnel every rate-limited endpoint on the
    platform resolves its rules through. A multiplier that were correct and
    unreached would be the most convincing kind of wrong.
    """

    def test_a_guard_scales_the_rules_its_module_declared(self) -> None:
        from app.modules.auth.presentation.rate_limits import LOGIN_RATE_LIMIT

        strict = LOGIN_RATE_LIMIT.rules(RateLimitSettings())
        relaxed = LOGIN_RATE_LIMIT.rules(
            RateLimitSettings().model_copy(update={"profile": RateLimitProfile.TEST})
        )

        assert {rule.name for rule in strict} == {rule.name for rule in relaxed}
        for tight, loose in zip(strict, relaxed, strict=True):
            assert loose.limit == tight.limit * 100
            assert loose.window == tight.window

    def test_a_policy_written_after_this_is_scaled_without_being_registered(self) -> None:
        """**§6, and the reason scaling lives in the guard.**

        A rule invented here has never been seen by any registry, table or
        mapping — and it is scaled anyway, because every guard resolves
        through one place. That is the property a second table of
        development numbers could not have: the failure mode there is a rule
        added today with a production figure and no development one, found
        by the person who added it being rate limited on their own laptop.
        """
        from datetime import timedelta as _timedelta

        from app.api.rate_limiting import RateLimit

        invented = RateLimitRule(
            name="something_invented_next_year",
            scope=RateLimitScope.USER,
            limit=3,
            window=_timedelta(minutes=1),
        )
        guard = RateLimit("invented", lambda _settings: [invented])

        relaxed = guard.rules(
            RateLimitSettings().model_copy(update={"profile": RateLimitProfile.DEVELOPMENT})
        )

        assert [rule.limit for rule in relaxed] == [3 * 20]


class TestTheLimiterStillLimits:
    def test_a_relaxed_profile_raises_the_ceiling_and_does_not_remove_it(self) -> None:
        """Scaling is not a kill switch.

        Every profile produces a finite limit and a real window, so the
        limiter is the same limiter and the counters are the same Redis
        counters — a runaway loop is still stopped on a laptop, which is
        worth keeping.

        `RateLimitRule.__post_init__` refuses a limit below one, so a
        profile that somehow multiplied by zero would fail loudly at
        construction rather than taking an endpoint down.
        """
        for profile in RateLimitProfile:
            relaxed = scaled(LOGIN_IP, profile)
            assert relaxed.limit >= 1
            assert relaxed.window > timedelta(0)

    def test_every_profile_has_a_multiplier(self) -> None:
        """A profile added without one fails at the lookup rather than
        defaulting to something plausible."""
        for profile in RateLimitProfile:
            assert profile.multiplier >= 1
