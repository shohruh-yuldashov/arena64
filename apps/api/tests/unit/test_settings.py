"""Settings and environment loading — dependency-injection.md §2, DI-06."""

import pytest
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from app.config.environment import Environment, current_environment, env_file_for
from app.config.settings import AppSettings, PostgresSettings, RedisSettings, Settings, get_settings


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
                ),
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
            ),
        )
        assert settings.environment is Environment.PRODUCTION

    def test_settings_are_immutable(self) -> None:
        settings = Settings(
            environment=Environment.TEST,
            app=AppSettings(),
            postgres=PostgresSettings(),
            redis=RedisSettings(),
        )
        with pytest.raises(PydanticValidationError):
            settings.environment = Environment.PRODUCTION  # type: ignore[misc]
