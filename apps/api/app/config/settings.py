"""Typed configuration — dependency-injection.md §2.1.

One class per module-prefixed section (`APP_*`, `POSTGRES_*`, `REDIS_*`),
composed into one immutable `Settings` object. A module never reads the
environment directly (dependency-injection.md §1.6); it receives this
object, or a section of it, injected.

`get_settings()` is the only place these are constructed. It is called once,
cached, and the result is frozen — dependency-injection.md DI-06: a missing
or malformed setting must abort the process before it accepts traffic, not
fail mysteriously on the ten-thousandth request.
"""

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.environment import Environment, current_environment, env_file_for

# Convenience defaults for `local` and `test`, where "it just runs" matters
# more than explicit configuration. Never valid for a deployed tier — see
# Settings._forbid_local_defaults_outside_local below, which is what makes
# this safe rather than merely convenient.
_LOCAL_POSTGRES_DSN = "postgresql+asyncpg://arena64:arena64@localhost:5432/arena64"
_LOCAL_REDIS_URLS = {
    "live": "redis://localhost:6379/0",
    "bus": "redis://localhost:6379/1",
    "broker": "redis://localhost:6379/2",
    "cache": "redis://localhost:6379/3",
}


class AppSettings(BaseSettings):
    """`app` — architecture.md §5 process identity and log posture."""

    model_config = SettingsConfigDict(env_prefix="APP_", frozen=True, extra="forbid")

    name: str = "arena64-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # None means "let the environment decide" (dependency-injection.md §2.3:
    # human-readable in `local`, JSON everywhere else) — set explicitly only
    # to override that default, e.g. JSON logs on a local machine to test a
    # log pipeline.
    log_format: Literal["json", "human"] | None = None


class PostgresSettings(BaseSettings):
    """`postgres` — one primary DSN.

    Read-replica routing (database.md §13.2) is not modelled here — no
    module reads through this settings object yet, so there is nothing to
    route. Adding replica DSNs belongs with the first module that needs
    them, not speculatively (CLAUDE.md §1 rule 7).
    """

    model_config = SettingsConfigDict(env_prefix="POSTGRES_", frozen=True, extra="forbid")

    dsn: SecretStr = SecretStr(_LOCAL_POSTGRES_DSN)
    pool_size: int = 10
    max_overflow: int = 5
    pool_timeout_seconds: int = 30
    statement_timeout_ms: int = 5000
    echo: bool = False


class RedisSettings(BaseSettings):
    """`redis` — four role-separated pools, never one shared client.

    architecture.md AD-03: a spectator fan-out storm on `bus` must not be
    able to evict live match state on `live`; a queue backlog on `broker`
    must not evict the leaderboard read model on `cache`. Four URLs, four
    independent pools (app/database/redis.py) — never a single client
    reused across roles, even where they happen to point at the same
    instance in `local`.
    """

    model_config = SettingsConfigDict(env_prefix="REDIS_", frozen=True, extra="forbid")

    live_url: SecretStr = SecretStr(_LOCAL_REDIS_URLS["live"])
    bus_url: SecretStr = SecretStr(_LOCAL_REDIS_URLS["bus"])
    broker_url: SecretStr = SecretStr(_LOCAL_REDIS_URLS["broker"])
    cache_url: SecretStr = SecretStr(_LOCAL_REDIS_URLS["cache"])


class AuthSettings(BaseSettings):
    """`auth` — Argon2id cost parameters (A64-011.1).

    Configurable rather than hardcoded because database.md §14.2 requires
    hardening to be raisable over time: "per-row parameters let a sign-in
    verify against the parameters the hash was made with and transparently
    rehash at the current settings — a rolling upgrade with no forced
    reset." A constant in code could only be raised by a deploy, and would
    silently age.

    Defaults are OWASP's second recommended Argon2id profile
    (m=19456 KiB, t=2, p=1) rather than argon2-cffi's own
    (m=65536, t=3, p=4). Two concrete reasons, not preference:

      **p=1** — parallelism inside a single hash competes with the worker
      threads `Argon2idPasswordHasher` already uses to keep hashing off
      the event loop. Parallelism *across* requests is what this workload
      needs; p=4 would multiply thread pressure per registration for no
      security gain over an equivalent-cost m/t profile.

      **m=19456 (19 MiB)** — memory cost is per concurrent hash. At 64 MiB
      a burst of 40 simultaneous registrations reserves ~2.5 GB; at 19 MiB
      the same burst is ~760 MB. Registration is a public, unauthenticated,
      rate-limit-less endpoint (see the task's recommendations), so the
      memory a burst can pin is an availability question, not just a
      security one.

    The `ge=` floors are there so a well-meant "let's speed up
    registration" cannot quietly drop the platform below a defensible cost.
    Lowering them is a security decision, not a tuning one.
    """

    model_config = SettingsConfigDict(env_prefix="AUTH_", frozen=True, extra="forbid")

    argon2_time_cost: int = Field(default=2, ge=1)
    argon2_memory_cost_kib: int = Field(default=19456, ge=8192)
    argon2_parallelism: int = Field(default=1, ge=1)


class Settings(BaseModel):
    """The composed, immutable configuration for this process."""

    model_config = ConfigDict(frozen=True)

    environment: Environment
    app: AppSettings
    postgres: PostgresSettings
    redis: RedisSettings
    auth: AuthSettings

    @model_validator(mode="after")
    def _forbid_local_defaults_outside_local(self) -> "Settings":
        """DI-06's enforcement point.

        The convenience defaults above are exactly the misconfiguration
        that must fail loudly: a deploy that silently points `staging` or
        `production` at `localhost` because a secret was never wired in.
        Refusing to start is a visible, automatically-rolled-back deploy
        failure; starting and serving traffic against `localhost` is an
        outage that takes live matches with it (system-design.md T-2).
        """
        if not self.environment.is_production_like:
            return self

        if self.postgres.dsn.get_secret_value() == _LOCAL_POSTGRES_DSN:
            raise ValueError(
                f"POSTGRES_DSN must be set explicitly in {self.environment} "
                "— refusing the local default"
            )

        redis_defaults = {
            "REDIS_LIVE_URL": (self.redis.live_url, _LOCAL_REDIS_URLS["live"]),
            "REDIS_BUS_URL": (self.redis.bus_url, _LOCAL_REDIS_URLS["bus"]),
            "REDIS_BROKER_URL": (self.redis.broker_url, _LOCAL_REDIS_URLS["broker"]),
            "REDIS_CACHE_URL": (self.redis.cache_url, _LOCAL_REDIS_URLS["cache"]),
        }
        unset = [
            name
            for name, (value, default) in redis_defaults.items()
            if value.get_secret_value() == default
        ]
        if unset:
            raise ValueError(
                f"{', '.join(unset)} must be set explicitly in {self.environment} "
                "— refusing the local default"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The single construction point. Cached: settings are immutable for the
    life of the process (dependency-injection.md DI-06) — re-reading per
    call would make configuration a moving target mid-request.
    """
    environment = current_environment()
    env_file = env_file_for(environment)

    # `_env_file` is a documented pydantic-settings initialiser argument,
    # but it is absorbed through `**values` rather than declared, so
    # Pyright cannot see it and reports `reportCallIssue`. mypy accepts it
    # (and rejects a `type: ignore` here as unused), so the suppression has
    # to be the Pyright-specific spelling — a `# type: ignore` would make
    # one checker pass and the other fail.
    return Settings(
        environment=environment,
        app=AppSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        postgres=PostgresSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        redis=RedisSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        auth=AuthSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
    )
