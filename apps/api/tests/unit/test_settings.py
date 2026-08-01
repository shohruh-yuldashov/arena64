"""Settings and environment loading — dependency-injection.md §2, DI-06."""

import pytest
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from app.config.environment import Environment, current_environment, env_file_for
from app.config.settings import (
    JWT_SECRET_MIN_LENGTH,
    REFRESH_TOKEN_MIN_ENTROPY_BYTES,
    SUPPORTED_JWT_ALGORITHMS,
    AppSettings,
    AuthSettings,
    EmailSettings,
    JWTSettings,
    PostgresSettings,
    RateLimitSettings,
    RedisSettings,
    SessionSettings,
    Settings,
    get_settings,
)

#: A key that is explicitly *not* the development default, so the
#: production tests below fail for the reason each one is about rather
#: than tripping the JWT guard first.
EXPLICIT_JWT_SECRET = "a-real-deployment-signing-key-well-over-the-minimum-length"


class TestEnvironment:
    def test_defaults_to_local_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        assert current_environment() is Environment.LOCAL

    def test_reads_the_environment_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENVIRONMENT", "staging")
        assert current_environment() is Environment.STAGING

    def test_rejects_an_unrecognised_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENVIRONMENT", "not-a-real-environment")
        with pytest.raises(ValueError, match="ENVIRONMENT"):
            current_environment()

    def test_only_local_reads_an_env_file(self) -> None:
        assert env_file_for(Environment.LOCAL) is not None
        assert env_file_for(Environment.TEST) is None
        assert env_file_for(Environment.PRODUCTION) is None

    @pytest.mark.parametrize(
        ("environment", "expected"),
        [
            # `local` runs against real, developer-owned infrastructure
            # (e.g. docker-compose) — it is not the "no real infrastructure"
            # case `is_test` identifies (app/config/environment.py).
            (Environment.LOCAL, False),
            (Environment.TEST, True),
            (Environment.CI, True),
            (Environment.STAGING, False),
            (Environment.PRODUCTION, False),
        ],
    )
    def test_is_test_matches_dependency_injection_doc_s_2_3(
        self, environment: Environment, expected: bool
    ) -> None:
        assert environment.is_test is expected


class TestSettings:
    def test_get_settings_reads_the_current_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENVIRONMENT", "test")
        assert get_settings().environment is Environment.TEST

    def test_get_settings_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENVIRONMENT", "test")
        assert get_settings() is get_settings()

    def test_local_default_dsn_is_accepted_outside_production_like_tiers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        settings = Settings(
            environment=Environment.TEST,
            app=AppSettings(),
            postgres=PostgresSettings(),
            redis=RedisSettings(),
            auth=AuthSettings(),
            jwt=JWTSettings(secret_key=SecretStr(EXPLICIT_JWT_SECRET)),
            session=SessionSettings(),
            email=EmailSettings(),
            rate_limit=RateLimitSettings(),
        )
        assert settings.environment is Environment.TEST

    def test_production_rejects_the_local_default_dsn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        with pytest.raises(PydanticValidationError, match="POSTGRES_DSN"):
            Settings(
                environment=Environment.PRODUCTION,
                app=AppSettings(),
                postgres=PostgresSettings(),  # left at the local default
                redis=RedisSettings(
                    live_url=SecretStr("redis://prod-live:6379/0"),
                    bus_url=SecretStr("redis://prod-bus:6379/0"),
                    broker_url=SecretStr("redis://prod-broker:6379/0"),
                    cache_url=SecretStr("redis://prod-cache:6379/0"),
                    limits_url=SecretStr("redis://prod-limits:6379/0"),
                ),
                auth=AuthSettings(),
                jwt=JWTSettings(secret_key=SecretStr(EXPLICIT_JWT_SECRET)),
                session=SessionSettings(),
                email=EmailSettings(),
                rate_limit=RateLimitSettings(),
            )

    def test_production_rejects_a_left_default_redis_role(self) -> None:
        with pytest.raises(PydanticValidationError, match="REDIS_"):
            Settings(
                environment=Environment.PRODUCTION,
                app=AppSettings(),
                postgres=PostgresSettings(
                    dsn=SecretStr("postgresql+asyncpg://real:pw@prod-host:5432/arena64")
                ),
                redis=RedisSettings(),  # every role left at its local default
                auth=AuthSettings(),
                jwt=JWTSettings(secret_key=SecretStr(EXPLICIT_JWT_SECRET)),
                session=SessionSettings(),
                email=EmailSettings(),
                rate_limit=RateLimitSettings(),
            )

    def test_production_accepts_fully_explicit_configuration(self) -> None:
        settings = Settings(
            environment=Environment.PRODUCTION,
            app=AppSettings(),
            postgres=PostgresSettings(
                dsn=SecretStr("postgresql+asyncpg://real:pw@prod-host:5432/arena64")
            ),
            redis=RedisSettings(
                live_url=SecretStr("redis://prod-live:6379/0"),
                bus_url=SecretStr("redis://prod-bus:6379/0"),
                broker_url=SecretStr("redis://prod-broker:6379/0"),
                cache_url=SecretStr("redis://prod-cache:6379/0"),
                limits_url=SecretStr("redis://prod-limits:6379/0"),
            ),
            auth=AuthSettings(),
            jwt=JWTSettings(secret_key=SecretStr(EXPLICIT_JWT_SECRET)),
            session=SessionSettings(),
            email=EmailSettings(),
            rate_limit=RateLimitSettings(),
        )
        assert settings.environment is Environment.PRODUCTION

    def test_settings_are_immutable(self) -> None:
        settings = Settings(
            environment=Environment.TEST,
            app=AppSettings(),
            postgres=PostgresSettings(),
            redis=RedisSettings(),
            auth=AuthSettings(),
            jwt=JWTSettings(secret_key=SecretStr(EXPLICIT_JWT_SECRET)),
            session=SessionSettings(),
            email=EmailSettings(),
            rate_limit=RateLimitSettings(),
        )
        with pytest.raises(PydanticValidationError):
            settings.environment = Environment.PRODUCTION  # type: ignore[misc]


class TestJWTSettings:
    """A64-011.3. Every assertion here is a misconfiguration that would be
    invisible at runtime: the service starts, serves traffic, and is
    quietly forgeable. Failing at construction is what turns each one into
    a deploy that rolls back (DI-06)."""

    def test_defaults_are_usable_without_configuration(self) -> None:
        settings = JWTSettings()

        assert settings.algorithm == "HS256"
        assert settings.access_token_ttl_seconds == 900
        assert settings.issuer and settings.audience

    def test_the_secret_does_not_render_in_a_repr(self) -> None:
        """dependency-injection.md §2.4 — a settings repr in a traceback is
        the most common leak path, and this is the one secret that would
        let a reader mint tokens for any account."""
        assert "insecure" not in repr(JWTSettings())

    @pytest.mark.parametrize(
        "algorithm",
        [
            pytest.param("none", id="none"),
            pytest.param("None", id="None-capitalised"),
            pytest.param("RS256", id="asymmetric"),
            pytest.param("HS255", id="typo"),
            pytest.param("", id="empty"),
        ],
    )
    def test_rejects_algorithms_outside_the_allowlist(self, algorithm: str) -> None:
        """`none` disables signing entirely; an asymmetric `alg` against a
        symmetric secret is the algorithm-confusion attack. Neither should
        be reachable by editing an environment variable."""
        with pytest.raises(PydanticValidationError, match="JWT_ALGORITHM"):
            JWTSettings(algorithm=algorithm)

    def test_accepts_every_algorithm_in_the_allowlist(self) -> None:
        for algorithm in SUPPORTED_JWT_ALGORITHMS:
            assert JWTSettings(algorithm=algorithm).algorithm == algorithm

    def test_rejects_a_secret_shorter_than_the_hash_it_keys(self) -> None:
        """RFC 7518 §3.2. A 12-character key does not make HS256 weak in an
        obvious way — it makes it weaker than advertised, silently."""
        short = SecretStr("x" * (JWT_SECRET_MIN_LENGTH - 1))
        with pytest.raises(PydanticValidationError, match="at least"):
            JWTSettings(secret_key=short)

    def test_rejects_a_short_key_among_the_previous_keys(self) -> None:
        """A rotation is exactly when a weak key gets pasted in by hand."""
        with pytest.raises(PydanticValidationError, match="PREVIOUS"):
            JWTSettings(
                secret_key=SecretStr("n" * 40),
                previous_secret_keys=(SecretStr("short"),),
            )

    def test_rejects_a_rotation_that_lists_the_current_key_as_previous(self) -> None:
        key = SecretStr("k" * 40)
        with pytest.raises(PydanticValidationError, match="not a rotation"):
            JWTSettings(secret_key=key, previous_secret_keys=(key,))

    def test_verification_keys_put_the_current_key_first(self) -> None:
        """So the common case — a token signed by the key in use — verifies
        on the first HMAC, and a rotation costs an extra one only for
        tokens that predate it."""
        current, previous = SecretStr("n" * 40), SecretStr("o" * 40)
        settings = JWTSettings(secret_key=current, previous_secret_keys=(previous,))

        assert settings.verification_keys == (current, previous)

    def test_the_lifetime_is_bounded_at_both_ends(self) -> None:
        """The upper bound is the security-relevant one: a stateless token
        cannot be revoked, so its lifetime *is* the window in which a
        suspension (SE-3) or a password change (SE-1) does not take
        effect. An hour is the most this platform will let that be."""
        with pytest.raises(PydanticValidationError):
            JWTSettings(access_token_ttl_seconds=3601)
        with pytest.raises(PydanticValidationError):
            JWTSettings(access_token_ttl_seconds=59)


class TestJWTProductionGuard:
    def _production(self, **jwt_overrides: object) -> Settings:
        return Settings(
            environment=Environment.PRODUCTION,
            app=AppSettings(),
            postgres=PostgresSettings(
                dsn=SecretStr("postgresql+asyncpg://real:pw@prod-host:5432/arena64")
            ),
            redis=RedisSettings(
                live_url=SecretStr("redis://prod-live:6379/0"),
                bus_url=SecretStr("redis://prod-bus:6379/0"),
                broker_url=SecretStr("redis://prod-broker:6379/0"),
                cache_url=SecretStr("redis://prod-cache:6379/0"),
                limits_url=SecretStr("redis://prod-limits:6379/0"),
            ),
            auth=AuthSettings(),
            session=SessionSettings(),
            email=EmailSettings(),
            rate_limit=RateLimitSettings(),
            jwt=JWTSettings(**jwt_overrides),  # type: ignore[arg-type]
        )

    def test_production_refuses_the_development_signing_key(self) -> None:
        """The most consequential of the three local-default guards. A
        wrong database URL fails loudly on the first query; this one fails
        nowhere — the service runs, and anyone with a copy of this
        repository can mint a valid token for any account on it."""
        with pytest.raises(PydanticValidationError, match="JWT_SECRET_KEY"):
            self._production()

    def test_production_accepts_an_explicit_signing_key(self) -> None:
        settings = self._production(secret_key=SecretStr(EXPLICIT_JWT_SECRET))

        assert settings.environment is Environment.PRODUCTION

    def test_the_development_key_is_still_fine_outside_production(self) -> None:
        """`local` and `test` must keep running with no configuration —
        that is the whole reason a default exists."""
        settings = Settings(
            environment=Environment.TEST,
            app=AppSettings(),
            postgres=PostgresSettings(),
            redis=RedisSettings(),
            auth=AuthSettings(),
            session=SessionSettings(),
            email=EmailSettings(),
            rate_limit=RateLimitSettings(),
            jwt=JWTSettings(),
        )

        assert settings.jwt.secret_key.get_secret_value()


class TestSessionSettings:
    """A64-011.4. Each assertion is a misconfiguration that would weaken
    refresh sessions without failing anywhere at runtime — the class DI-06
    exists to turn into a deploy that rolls back."""

    def test_defaults_match_the_specified_policy(self) -> None:
        settings = SessionSettings()

        assert settings.refresh_token_ttl_days == 30
        assert settings.token_entropy_bytes == REFRESH_TOKEN_MIN_ENTROPY_BYTES

    def test_entropy_cannot_be_lowered_below_256_bits(self) -> None:
        """DB-24's whole argument for hashing refresh tokens with SHA-256
        rather than Argon2id is that a 256-bit random token has no
        guessable space. Below that the argument stops holding, and the
        hashing choice silently becomes wrong."""
        with pytest.raises(PydanticValidationError):
            SessionSettings(token_entropy_bytes=REFRESH_TOKEN_MIN_ENTROPY_BYTES - 1)

    def test_entropy_may_be_raised(self) -> None:
        assert SessionSettings(token_entropy_bytes=64).token_entropy_bytes == 64

    def test_the_absolute_lifetime_is_bounded(self) -> None:
        """The upper bound is the security-relevant one: this is how long
        a captured token stays useful if reuse detection never fires."""
        with pytest.raises(PydanticValidationError):
            SessionSettings(refresh_token_ttl_days=91)
        with pytest.raises(PydanticValidationError):
            SessionSettings(refresh_token_ttl_days=0)

    def test_an_idle_window_longer_than_the_absolute_one_is_refused(self) -> None:
        """It could never be the binding constraint, so configuring it that
        way does not add a second guard — it silently removes one."""
        with pytest.raises(PydanticValidationError, match="silently disables"):
            SessionSettings(refresh_token_ttl_days=30, idle_timeout_days=31)

    def test_equal_windows_are_allowed(self) -> None:
        """Degenerate but coherent: the idle guard simply never binds
        before the absolute one. Refusing it would be arbitrary."""
        settings = SessionSettings(refresh_token_ttl_days=30, idle_timeout_days=30)

        assert settings.idle_timeout_days == 30


class TestEmailSettings:
    """A64-011.6."""

    def test_defaults_match_the_specified_policy(self) -> None:
        settings = EmailSettings()

        assert settings.verification_token_ttl_hours == 24
        assert settings.token_entropy_bytes == REFRESH_TOKEN_MIN_ENTROPY_BYTES

    def test_entropy_cannot_be_lowered_below_256_bits(self) -> None:
        """DB-24's premise: below 256 bits, hashing with SHA-256 rather
        than Argon2id stops being sound."""
        with pytest.raises(PydanticValidationError):
            EmailSettings(token_entropy_bytes=REFRESH_TOKEN_MIN_ENTROPY_BYTES - 1)

    def test_the_lifetime_is_bounded(self) -> None:
        with pytest.raises(PydanticValidationError):
            EmailSettings(verification_token_ttl_hours=0)
        with pytest.raises(PydanticValidationError):
            EmailSettings(verification_token_ttl_hours=169)

    def test_a_url_template_without_the_placeholder_is_refused(self) -> None:
        """Every link would otherwise be identical and none would work —
        and nothing would fail until a real person clicked one."""
        with pytest.raises(PydanticValidationError, match="token"):
            EmailSettings(verification_url_template="https://arena64.example/verify")

    def test_the_url_carries_the_token(self) -> None:
        settings = EmailSettings(verification_url_template="https://arena64.example/v?t={token}")

        assert settings.verification_url("abc") == "https://arena64.example/v?t=abc"


class TestConsoleEmailProviderGuard:
    """The provider writes verification links to the log — that is what it
    is for. The guard is what keeps that from ever happening on a deployed
    tier."""

    @pytest.mark.parametrize(
        "environment",
        [Environment.LOCAL, Environment.TEST, Environment.CI],
    )
    def test_constructs_outside_production(self, environment: Environment) -> None:
        from app.modules.auth.infrastructure import ConsoleEmailProvider

        assert ConsoleEmailProvider(environment)

    @pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
    def test_refuses_to_construct_in_a_deployed_tier(self, environment: Environment) -> None:
        """A deployed tier wired to this provider would send nobody
        anything *and* write live links into the log pipeline. Refusing to
        start is a visible deploy failure; starting is a silent one."""
        from app.modules.auth.infrastructure import ConsoleEmailProvider

        with pytest.raises(ValueError, match="ConsoleEmailProvider"):
            ConsoleEmailProvider(environment)
