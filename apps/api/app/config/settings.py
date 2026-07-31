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
_LOCAL_JWT_SECRET_KEY = (
    # 64 characters, so it clears `JWT_SECRET_MIN_LENGTH` and `local` runs
    # with no configuration at all. The literal words are load-bearing: if
    # this ever reaches a deployed tier the guard below names it in the
    # crash, and anyone reading a leaked token's key sees immediately that
    # it was never a secret.
    "insecure-local-development-key-do-not-use-outside-local-0123456"
)
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


#: HMAC only, and deliberately a closed list rather than "whatever PyJWT
#: supports". The classic JWT break is algorithm confusion: a service
#: configured with a *symmetric* secret but willing to accept an asymmetric
#: `alg` lets an attacker sign tokens with the public key — which is
#: public. Refusing anything but HMAC at configuration time means that
#: mistake cannot be made by editing an environment variable.
#:
#: `none` is absent for the obvious reason and cannot be reintroduced: the
#: field is validated against this set, and `JwtTokenProvider` passes the
#: allowlist to PyJWT's `algorithms=` argument, which is what actually
#: stops a token's own header from choosing its verification algorithm.
SUPPORTED_JWT_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})

#: RFC 7518 §3.2: an HMAC key "MUST have a size >= the size of the hash
#: output". For HS256 that is 32 bytes. 32 is therefore the floor, not a
#: preference — a shorter key weakens HS256 below its nominal strength.
JWT_SECRET_MIN_LENGTH = 32


class JWTSettings(BaseSettings):
    """`jwt` — access token signing and verification (A64-011.3).

    ## Why the lifetime default is 15 minutes

    A signed JWT is a *bearer* credential that the server does not store,
    so between issue and expiry there is nothing to revoke — the token is
    valid because it verifies, not because a row says so. domain-model.md
    SE-1 and SE-3 require that a password change and a suspension revoke
    access *immediately*; a stateless token cannot honour that, and the
    only lever left is the length of the window in which it is wrong.

    Fifteen minutes is that lever set to a defensible value: short enough
    that a suspended account's remaining reach is bounded and a stolen
    token has little resale value, long enough that reissue is not a
    per-request cost. A64-011.4's refresh tokens are what make it
    comfortable rather than merely correct — they move the long-lived
    credential into a *stored* record that genuinely can be revoked.

    Raising this past an hour should be treated as a security decision:
    it directly extends how long a revoked session keeps working.

    ## Why there are two key fields

    dependency-injection.md §2.4 requires signing keys to be "rotatable
    without downtime", and makes the argument concretely for the WebSocket
    ticket key: single-key rotation invalidates every credential in flight
    at the instant of rotation. The same reasoning applies here and lands
    harder — rotating a single JWT key signs every user out at once, so
    rotation becomes an incident and therefore never happens.

    `secret_key` signs; `secret_key` **and** `previous_secret_keys` verify.
    A rotation is: publish the new key as `secret_key`, move the old one to
    `previous_secret_keys`, and drop it after one token lifetime. Nobody
    is signed out, and the window in which the old key still verifies is
    bounded by `access_token_ttl_seconds` rather than by a deploy.
    """

    model_config = SettingsConfigDict(env_prefix="JWT_", frozen=True, extra="forbid")

    secret_key: SecretStr = SecretStr(_LOCAL_JWT_SECRET_KEY)

    #: Keys that still verify but no longer sign. Empty in steady state;
    #: non-empty only during a rotation window.
    previous_secret_keys: tuple[SecretStr, ...] = ()

    algorithm: str = "HS256"

    #: 15 minutes. See this class's docstring — this is the revocation
    #: window, not a performance tuning knob.
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    #: `iss` and `aud`. Both are verified on every decode, and both exist
    #: to stop a token minted for one purpose being replayed at another:
    #: once `auth` also mints WebSocket tickets (AD-09) and, later, tokens
    #: for a mobile client, "signed by us" stops being sufficient proof
    #: that a token was meant for *this* verifier.
    issuer: str = Field(default="arena64", min_length=1)
    audience: str = Field(default="arena64-api", min_length=1)

    @model_validator(mode="after")
    def _validate_algorithm_and_keys(self) -> "JWTSettings":
        if self.algorithm not in SUPPORTED_JWT_ALGORITHMS:
            raise ValueError(
                f"JWT_ALGORITHM must be one of {sorted(SUPPORTED_JWT_ALGORITHMS)}; "
                f"got {self.algorithm!r}. Asymmetric algorithms are refused "
                "deliberately — this platform signs with a symmetric secret, and "
                "accepting an asymmetric `alg` is the algorithm-confusion attack."
            )

        for label, key in self._all_keys():
            if len(key.get_secret_value()) < JWT_SECRET_MIN_LENGTH:
                raise ValueError(
                    f"{label} must be at least {JWT_SECRET_MIN_LENGTH} characters "
                    "(RFC 7518 §3.2: an HMAC key must be at least as long as the "
                    "hash it keys)"
                )

        # A rotation that lists the current key as a previous one is
        # harmless but always a mistake — it means the operator believes a
        # rotation is in progress when it is not.
        current = self.secret_key.get_secret_value()
        if any(key.get_secret_value() == current for key in self.previous_secret_keys):
            raise ValueError(
                "JWT_PREVIOUS_SECRET_KEYS must not contain JWT_SECRET_KEY — "
                "a rotation window with the same key on both sides is not a rotation"
            )
        return self

    def _all_keys(self) -> list[tuple[str, SecretStr]]:
        return [("JWT_SECRET_KEY", self.secret_key)] + [
            (f"JWT_PREVIOUS_SECRET_KEYS[{index}]", key)
            for index, key in enumerate(self.previous_secret_keys)
        ]

    @property
    def verification_keys(self) -> tuple[SecretStr, ...]:
        """Every key a token may have been signed with, newest first.

        Ordered so the overwhelmingly common case — a token signed by the
        current key — verifies on the first attempt, and a rotation costs
        one extra HMAC only for tokens that predate it.
        """
        return (self.secret_key, *self.previous_secret_keys)


#: 256 bits, per the task's floor and RFC 4086's guidance for a value
#: whose only defence is being unguessable. A refresh token is not
#: stretched — DB-24 hashes it with SHA-256 rather than Argon2id, and that
#: is only sound because the token has no guessable space to defend. The
#: entropy *is* the security, so the floor is not negotiable downward.
REFRESH_TOKEN_MIN_ENTROPY_BYTES = 32


class SessionSettings(BaseSettings):
    """`session` — refresh token lifetime and entropy (A64-011.4).

    ## Why two expiries rather than one

    database.md §14.3 requires "absolute and idle expiry both", and §4.4
    gives the reason plainly: an idle expiry alone lets a stolen token be
    kept alive indefinitely by using it, while an absolute expiry alone
    logs out a daily player mid-session. Together they bound the damage of
    theft without punishing normal use.

    The task specifies one `expires_at` column and a 30-day lifetime, so
    that is the absolute expiry and it is the column. Idle expiry needs no
    column of its own — it is `last_used_at + idle_timeout_days`, and
    `last_used_at` is already in the table. One stored value, both
    guarantees.

    ## Why 30 days is a real decision

    This is the outer bound on how long a captured refresh token remains
    useful if reuse detection never fires — that is, if the attacker
    captures a token the legitimate user never presents again. Thirty days
    is the task's figure and is defensible for a game where a lapsed
    player returning after three weeks should not have to re-authenticate.
    It is also long, and it is why rotation and reuse detection are not
    optional extras here: they are what actually bounds the exposure.
    """

    model_config = SettingsConfigDict(env_prefix="SESSION_", frozen=True, extra="forbid")

    refresh_token_ttl_days: int = Field(default=30, ge=1, le=90)

    #: How long a session may sit unused before it stops being valid,
    #: independent of `refresh_token_ttl_days`. Shorter than the absolute
    #: window by construction — a validator below enforces that, because
    #: an idle window longer than the absolute one is not a second guard,
    #: it is a disabled one.
    idle_timeout_days: int = Field(default=14, ge=1, le=90)

    #: Bytes drawn from the OS CSPRNG per token. The floor is a security
    #: boundary, not a tuning knob — see `REFRESH_TOKEN_MIN_ENTROPY_BYTES`.
    token_entropy_bytes: int = Field(
        default=REFRESH_TOKEN_MIN_ENTROPY_BYTES, ge=REFRESH_TOKEN_MIN_ENTROPY_BYTES
    )

    @model_validator(mode="after")
    def _idle_must_be_shorter_than_absolute(self) -> "SessionSettings":
        if self.idle_timeout_days > self.refresh_token_ttl_days:
            raise ValueError(
                "SESSION_IDLE_TIMEOUT_DAYS must not exceed "
                "SESSION_REFRESH_TOKEN_TTL_DAYS — an idle window longer than the "
                "absolute one can never be the binding constraint, so configuring "
                "it that way silently disables the idle guard"
            )
        return self


class Settings(BaseModel):
    """The composed, immutable configuration for this process."""

    model_config = ConfigDict(frozen=True)

    environment: Environment
    app: AppSettings
    postgres: PostgresSettings
    redis: RedisSettings
    auth: AuthSettings
    jwt: JWTSettings
    session: SessionSettings

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

        # The most consequential of the three. A deployed tier running on
        # the development signing key does not merely leak — it lets
        # anyone holding this repository mint a valid token for any
        # account on the platform, because the key is right there in the
        # source. Unlike a wrong database URL, nothing about it fails
        # visibly: the service starts, serves traffic, and is silently
        # unauthenticated.
        if self.jwt.secret_key.get_secret_value() == _LOCAL_JWT_SECRET_KEY:
            raise ValueError(
                f"JWT_SECRET_KEY must be set explicitly in {self.environment} "
                "— refusing the development default, which is published in the "
                "repository and would let anyone forge tokens for any account"
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
        jwt=JWTSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        session=SessionSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
    )
