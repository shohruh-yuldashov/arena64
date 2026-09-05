"""Where a transactional email's links point — A64-028.2 §16–§19.

A64-028.1 P0-1: both templates defaulted to `http://localhost:3000` and
were the only local defaults `Settings` did not refuse in a deployed tier.
A staging or production process that set `PUBLIC_APP_URL` and nothing else
— which is exactly what `infrastructure/staging/compose.yml` does — started
normally and sent every verification and reset link to a machine the
recipient does not have. Nothing failed; the mail arrived; the links were
dead.

These tests hold the fix from both ends: the links are *derived* from the
one configured origin, and an explicitly configured loopback is refused.
"""

import pytest
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from app.config.environment import Environment
from app.config.settings import (
    AppSettings,
    AuthSettings,
    BrowserSessionSettings,
    EmailSettings,
    FriendsSettings,
    GameSettings,
    GatewaySettings,
    JWTSettings,
    MatchmakingSettings,
    NotificationEmailSettings,
    OutboxSettings,
    PostgresSettings,
    PresenceSettings,
    PushSettings,
    RateLimitSettings,
    RedisSettings,
    SessionSettings,
    Settings,
    StatisticsSettings,
    StorageSettings,
    TournamentSettings,
)

DEPLOYED_JWT_SECRET = "a-real-deployment-signing-key-well-over-the-minimum-length"
DEPLOYED_OTP_SECRET = "a-real-deployment-otp-secret-well-over-the-minimum-length"
DEPLOYED_ORIGIN = "https://arena64.gg"
DEPLOYED_DSN = "postgresql+asyncpg://a:b@db:5432/arena64"


def build(
    environment: Environment, *, public_url: str, email: EmailSettings | None = None
) -> Settings:
    """A whole `Settings`, because the resolution under test is a property of
    the composition rather than of `EmailSettings` alone: the origin and the
    links live in different sections and only `Settings` holds both."""
    deployed = environment.is_production_like
    return Settings(
        environment=environment,
        app=AppSettings(public_url=public_url),
        postgres=PostgresSettings(dsn=SecretStr(DEPLOYED_DSN)) if deployed else PostgresSettings(),
        redis=RedisSettings(
            live_url=SecretStr("redis://cache:6379/0"),
            bus_url=SecretStr("redis://cache:6379/1"),
            broker_url=SecretStr("redis://cache:6379/2"),
            cache_url=SecretStr("redis://cache:6379/3"),
            limits_url=SecretStr("redis://cache:6379/4"),
        )
        if deployed
        else RedisSettings(),
        auth=AuthSettings(),
        jwt=JWTSettings(secret_key=SecretStr(DEPLOYED_JWT_SECRET)),
        session=SessionSettings(),
        email=email if email is not None else deployed_email(),
        notification_email=NotificationEmailSettings(),
        push=PushSettings(),
        storage=StorageSettings(),
        rate_limit=RateLimitSettings(),
        statistics=StatisticsSettings(),
        presence=PresenceSettings(),
        friends=FriendsSettings(),
        outbox=OutboxSettings(),
        matchmaking=MatchmakingSettings(),
        gateway=GatewaySettings(),
        game=GameSettings(),
        tournament=TournamentSettings(),
        browser_session=BrowserSessionSettings(
            trusted_origins=(DEPLOYED_ORIGIN,) if deployed else ()
        ),
    )


def deployed_email(
    *, verification_url_template: str | None = None, password_reset_url_template: str | None = None
) -> EmailSettings:
    """`EmailSettings` with a real OTP secret, and a template only where a
    test is about one — leaving the rest to be composed from the origin."""
    if verification_url_template is not None and password_reset_url_template is not None:
        return EmailSettings(
            otp_secret=SecretStr(DEPLOYED_OTP_SECRET),
            verification_url_template=verification_url_template,
            password_reset_url_template=password_reset_url_template,
        )
    if verification_url_template is not None:
        return EmailSettings(
            otp_secret=SecretStr(DEPLOYED_OTP_SECRET),
            verification_url_template=verification_url_template,
        )
    if password_reset_url_template is not None:
        return EmailSettings(
            otp_secret=SecretStr(DEPLOYED_OTP_SECRET),
            password_reset_url_template=password_reset_url_template,
        )
    return EmailSettings(otp_secret=SecretStr(DEPLOYED_OTP_SECRET))


class TestDerivation:
    def test_both_links_come_from_the_configured_origin(self) -> None:
        settings = build(Environment.PRODUCTION, public_url=DEPLOYED_ORIGIN)

        assert settings.email.verification_url("T0KEN") == (
            "https://arena64.gg/verify-email?token=T0KEN"
        )
        assert settings.email.password_reset_url("T0KEN") == (
            "https://arena64.gg/reset-password?token=T0KEN"
        )

    def test_local_development_is_unchanged(self) -> None:
        # The whole change is invisible on a laptop: `PUBLIC_APP_URL`'s local
        # default is the origin the templates used to hardcode, so the links
        # are byte-identical to the ones A64-011.6 shipped.
        settings = build(Environment.LOCAL, public_url="http://localhost:3000")

        assert settings.email.verification_url("T") == (
            "http://localhost:3000/verify-email?token=T"
        )
        assert settings.email.password_reset_url("T") == (
            "http://localhost:3000/reset-password?token=T"
        )

    def test_an_explicit_template_still_wins(self) -> None:
        # A mobile build pointing at a deep link is why these are templates
        # and not a base URL. That reason has not changed.
        settings = build(
            Environment.PRODUCTION,
            public_url=DEPLOYED_ORIGIN,
            email=deployed_email(verification_url_template="https://app.arena64.gg/v/{token}"),
        )

        assert settings.email.verification_url("T") == "https://app.arena64.gg/v/T"
        # …and the one left unset still follows the canonical origin.
        assert settings.email.password_reset_url("T").startswith(DEPLOYED_ORIGIN)


class TestDeployedTierRefusesLoopback:
    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "0.0.0.0", "[::1]"], ids=str)
    @pytest.mark.parametrize(
        "field",
        ["verification_url_template", "password_reset_url_template"],
    )
    @pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
    def test_an_explicit_loopback_link_refuses_to_start(
        self, environment: Environment, field: str, host: str
    ) -> None:
        # Derivation makes the *default* safe; this makes the override safe.
        # Checked by host, so a port or a path cannot smuggle one past.
        with pytest.raises(PydanticValidationError, match="EMAIL_"):
            build(
                environment,
                public_url=DEPLOYED_ORIGIN,
                email=deployed_email(**{field: f"http://{host}:3000/x?token={{token}}"}),
            )

    def test_a_real_origin_is_accepted(self) -> None:
        settings = build(
            Environment.PRODUCTION,
            public_url=DEPLOYED_ORIGIN,
            email=deployed_email(
                password_reset_url_template="https://arena64.gg/reset?token={token}"
            ),
        )

        assert settings.email.password_reset_url("T") == "https://arena64.gg/reset?token=T"

    def test_local_may_still_link_to_a_laptop(self) -> None:
        # The guard is about deployed tiers. A developer's machine is the one
        # place `localhost` is the correct answer.
        settings = build(
            Environment.LOCAL,
            public_url="http://localhost:3000",
            email=EmailSettings(
                verification_url_template="http://localhost:5173/verify?token={token}"
            ),
        )

        assert settings.email.verification_url("T") == "http://localhost:5173/verify?token=T"


class TestTemplateShape:
    def test_a_template_without_the_placeholder_is_still_refused(self) -> None:
        # A64-011.6's check, unchanged: every link would be identical and
        # none of them would work.
        with pytest.raises(PydanticValidationError, match="EMAIL_VERIFICATION_URL_TEMPLATE"):
            deployed_email(verification_url_template="https://arena64.gg/verify")

    def test_the_section_still_stands_on_its_own(self) -> None:
        # The composition in `Settings` is a *default*, not a dependency:
        # `EmailSettings` is constructed alone all over this suite and by
        # anything holding one section, and making it unusable without the
        # whole settings object would have been an interface break rather
        # than a fix (CLAUDE.md §7.7).
        assert EmailSettings().verification_url("T") == (
            "http://localhost:3000/verify-email?token=T"
        )
        assert EmailSettings().password_reset_url("T") == (
            "http://localhost:3000/reset-password?token=T"
        )

    def test_an_explicit_local_template_is_not_quietly_corrected(self) -> None:
        # `model_fields_set`, not a comparison against the local default: a
        # deployed tier that explicitly configured localhost is told, rather
        # than silently given something else. The refusal is the message.
        with pytest.raises(PydanticValidationError, match="EMAIL_VERIFICATION_URL_TEMPLATE"):
            build(
                Environment.PRODUCTION,
                public_url=DEPLOYED_ORIGIN,
                email=deployed_email(
                    verification_url_template="http://localhost:3000/verify-email?token={token}"
                ),
            )
