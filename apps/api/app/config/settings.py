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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from app.config.environment import Environment, current_environment, env_file_for
from app.core.rate_limiting import RateLimitProfile


def rate_limit_profile_for(environment: Environment) -> RateLimitProfile:
    """How hard rate limits bite in this environment — A64-021.6 §5.

    The **one** place the mapping lives, so a new policy anywhere on the
    platform is scaled by having been written at all, rather than by
    somebody remembering to add it to a second table.

    ## Fail-closed on anything unrecognised

    `.get(..., PRODUCTION)` rather than an exhaustive match that raises. An
    environment nobody thought about gets **production** limits, which is
    the direction a mistake here has to fail in: the alternative is a new
    tier silently shipping with hundred-fold allowances, and nothing about
    it would look wrong.

    Raising instead was considered and rejected for the same reason a
    missing translation renders a key rather than a blank page — a startup
    crash on an unrecognised environment name is a worse outcome than
    correct-and-strict behaviour, and `Environment` is a closed enum anyway,
    so the branch is reachable only by adding a member.
    """
    return _PROFILE_BY_ENVIRONMENT.get(environment, RateLimitProfile.PRODUCTION)


#: The mapping, and every member of `Environment` is listed deliberately —
#: including the two that map to `PRODUCTION`, so that "staging is strict" is
#: something a reader can see rather than infer from an absence.
_PROFILE_BY_ENVIRONMENT: dict[Environment, RateLimitProfile] = {
    Environment.LOCAL: RateLimitProfile.DEVELOPMENT,
    Environment.TEST: RateLimitProfile.TEST,
    Environment.CI: RateLimitProfile.TEST,
    Environment.STAGING: RateLimitProfile.PRODUCTION,
    Environment.PRODUCTION: RateLimitProfile.PRODUCTION,
}

# Convenience defaults for `local` and `test`, where "it just runs" matters
# more than explicit configuration. Never valid for a deployed tier — see
# Settings._forbid_local_defaults_outside_local below, which is what makes
# this safe rather than merely convenient.
#: Port 55432, matching docker/docker-compose.yml. Not 5432, because a
#: system Postgres on the standard port is the common case and the
#: container must not have to fight it for the binding.
_LOCAL_POSTGRES_DSN = "postgresql+asyncpg://arena64:arena64@localhost:55432/arena64"
_LOCAL_JWT_SECRET_KEY = (
    # 64 characters, so it clears `JWT_SECRET_MIN_LENGTH` and `local` runs
    # with no configuration at all. The literal words are load-bearing: if
    # this ever reaches a deployed tier the guard below names it in the
    # crash, and anyone reading a leaked token's key sees immediately that
    # it was never a secret.
    "insecure-local-development-key-do-not-use-outside-local-0123456"
)
#: The frontend a developer runs. Refused in a deployed tier by the guard on
#: `Settings`, for the reason that guard gives.
_LOCAL_PUBLIC_APP_URL = "http://localhost:3000"

_LOCAL_OTP_SECRET = (
    # Same shape and same reasoning as `_LOCAL_JWT_SECRET_KEY`: the literal
    # words are load-bearing, so a deployed tier that somehow reached it is
    # named in the crash and anybody reading a leaked verifier's key sees
    # immediately that it was never a secret.
    "insecure-local-development-otp-secret-do-not-use-outside-local-01"
)

_LOCAL_REDIS_URLS = {
    "live": "redis://localhost:6379/0",
    "bus": "redis://localhost:6379/1",
    "broker": "redis://localhost:6379/2",
    "cache": "redis://localhost:6379/3",
    "limits": "redis://localhost:6379/4",
}


def _require_bare_origin(value: str, *, variable: str) -> str:
    """A scheme and a host, and nothing else.

    Shared by `PUBLIC_APP_URL` and by the two `auth` URL templates' origin
    check, so "what counts as an origin" is decided once rather than in each
    validator that happens to need it.
    """
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"{variable} must start with http:// or https://")
    if value.endswith("/") or "/" in value.split("://", 1)[1]:
        raise ValueError(f"{variable} must be a bare origin — no trailing slash and no path")
    return value


class SectionSettings(BaseSettings):
    """Every settings section on this platform, with one behaviour added.

    ## The defect this exists for

    `pydantic-settings` filters **process environment variables** by
    `env_prefix` before validating them, and does not filter a **dotenv
    file** at all: every key in the file is offered to every model. With
    `extra="forbid"` — which every section here sets, deliberately — that
    means a `.env.local` containing `APP_LOG_LEVEL` makes `PostgresSettings`
    refuse to construct, because `app_log_level` is not one of its fields.

    The consequence was that `.env.local` had **never worked**, for anything.
    `.env.example`'s first line says "Copy to .env.local for local
    development"; doing so produced a crash on the first key belonging to
    another section, and the only reason nobody hit it is that local
    development runs on the code-level defaults.

    It surfaced as a Resend credential that would not load, which was two
    problems wearing one coat: a file named `.env` (nothing reads that — see
    `env_file_for`) and this.

    ## The fix, and what it deliberately keeps

    The dotenv source is wrapped so a section sees only the keys that are
    **its own** — its field names, and any `validation_alias` a field
    declares. Everything else is another section's business.

    `extra="forbid"` stays, and stays meaningful where it matters: the
    process-environment source is untouched, so a typo'd `POSTGRES_DSNN` in
    a deployed tier is still a refusal to start (DI-06). What is relaxed is
    only the local file, where the same typo is a line a developer can read
    beside the correct one in `.env.example`.
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """The default order, with the dotenv source narrowed to this section.

        Order is unchanged and is the layering `dependency-injection.md` §2.2
        describes: init < dotenv < environment < secrets, read right to left
        by precedence.
        """
        return (
            init_settings,
            env_settings,
            _SectionDotEnv(dotenv_settings, settings_cls),
            file_secret_settings,
        )


class _SectionDotEnv(PydanticBaseSettingsSource):
    """A dotenv source that yields only one section's keys.

    Wraps rather than replaces, so the file parsing, the encoding handling
    and the prefix stripping all stay `pydantic-settings`'. What is added is
    one filter.
    """

    def __init__(self, inner: PydanticBaseSettingsSource, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._inner = inner

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Never called: `__call__` below is overridden and does not delegate
        # to the per-field protocol. Present because the abstract base
        # declares it.
        raise NotImplementedError  # pragma: no cover

    def __call__(self) -> dict[str, Any]:
        accepted = _accepted_keys(self.settings_cls)
        return {key: value for key, value in self._inner().items() if key in accepted}


def _accepted_keys(settings_cls: type[BaseSettings]) -> frozenset[str]:
    """What one section may take from a shared file.

    Two forms, because the dotenv source emits two. Given a file holding
    `RESEND_API_KEY`, `EMAIL_FROM_NAME` and `APP_LOG_LEVEL`, it offers
    `EmailSettings`:

        RESEND_API_KEY   a declared `validation_alias`, **verbatim**
        from_name        its own prefixed key, stripped and lowercased
        app_log_level    another section's key, lowercased and passed through
        public_app_url   another section's alias, likewise

    The first two are this section's and the last two are not, so the
    accepted set is field names plus alias names **as written**. Lowercasing
    the aliases — the obvious thing, and what the first attempt did — admits
    exactly the pass-throughs it should exclude and drops the verbatim key it
    should keep.
    """
    keys = set(settings_cls.model_fields)
    for field in settings_cls.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, str):
            keys.add(alias)
    return frozenset(keys)


class AppSettings(SectionSettings):
    """`app` — architecture.md §5 process identity and log posture."""

    model_config = SettingsConfigDict(
        env_prefix="APP_", frozen=True, extra="forbid", populate_by_name=True
    )

    name: str = "arena64-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # None means "let the environment decide" (dependency-injection.md §2.3:
    # human-readable in `local`, JSON everywhere else) — set explicitly only
    # to override that default, e.g. JSON logs on a local machine to test a
    # log pipeline.
    log_format: Literal["json", "human"] | None = None

    public_url: str = Field(default=_LOCAL_PUBLIC_APP_URL, validation_alias="PUBLIC_APP_URL")
    """Where this platform lives, as a player types it — `PUBLIC_APP_URL`.

    Aliased past this class's `APP_` prefix, because the origin is a
    platform-wide fact rather than an API-process one and `APP_PUBLIC_URL`
    would read as the latter.

    **The canonical frontend origin, and there is exactly one.** A64-021.5
    put the notification email's origin on `NotificationEmailSettings`, and
    the continuation moved it here for the reason a second copy always
    justifies: `auth`'s two URL templates carry an origin too, and three
    settings holding the same host is three chances for an email to link a
    player into the wrong tier.

    A bare scheme and host — no trailing slash, no path — checked at
    construction, because both failures are silent: a trailing slash renders
    `https://x//tournaments`, and a path renders a link into the wrong part
    of the app. The mail sends and looks fine either way.

    Production is `https://arena64.gg`, and it is deliberately **not** the
    default: a default naming the real origin would make a misconfigured
    staging deploy send people into production. `Settings` refuses to start
    on the localhost default in a production-like tier — see
    `_forbid_local_defaults_outside_local`.
    """

    metrics_flush_interval_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    """How often accumulated counters are emitted — A64-015.6 §6.

    A minute, and the trade is resolution against volume: every counter on
    the platform becomes at most one record per series per interval, so this
    is directly the number of log lines metrics cost. At sixty seconds the
    pairing scan's four series cost four records a minute instead of the
    ~1.2 million a day a per-measurement recorder would write.

    A minute is also the coarsest useful bucket for the things being counted
    — a rate over scans, exclusions and reconciliation actions — and nothing
    on this platform needs sub-minute resolution on a counter. Observations
    are unaffected: they are emitted as they happen, because a summarised
    latency is not a latency.

    The floor is a guard against a configuration that turns the flush into a
    busy loop, not a tuning range.
    """

    @model_validator(mode="after")
    def _public_url_must_be_an_origin(self) -> "AppSettings":
        _require_bare_origin(self.public_url, variable="PUBLIC_APP_URL")
        return self


class PostgresSettings(SectionSettings):
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


class RedisSettings(SectionSettings):
    """`redis` — five role-separated pools, never one shared client.

    architecture.md AD-03: a spectator fan-out storm on `bus` must not be
    able to evict live match state on `live`; a queue backlog on `broker`
    must not evict the leaderboard read model on `cache`. One URL per role,
    one independent pool each (app/database/redis.py) — never a single
    client reused across roles, even where they happen to point at the same
    instance in `local`.

    ## Why A64-011.8 added a fifth role rather than using `cache`

    AD-03 names four. Rate limit counters are the fifth, and putting them
    on any of the existing four would break the decision rather than
    reuse it.

    **Not `cache`.** AD-03 states its persistence posture outright — "the
    cache runs with no persistence at all" — and a cache is configured with
    an eviction policy, because evicting from a cache is *correct*. A rate
    limit counter evicted under memory pressure is a limit that silently
    stops applying, and memory pressure on the cache arrives during a
    traffic spike, which is exactly when an authentication endpoint is
    either genuinely busy or under attack. The failure mode is a limiter
    that works in testing and disappears in the incident it exists for.

    **Not `live` or `bus`.** Both are AD-03's own example of a hostile
    interaction: a flood of login attempts is precisely the traffic that
    must not compete for memory or connections with games in progress. A
    limiter sharing an instance with live match state means a
    credential-stuffing run degrades matches, which is the platform-wide
    outage AD-03 exists to convert into a single degraded feature.

    **Not `broker`.** Celery owns the keyspace there.

    So this is AD-03 applied, not AD-03 amended: the argument for four
    instances is an argument about hostile workloads with different
    persistence needs, and rate limiting is a sixth such workload with a
    seventh such need (it wants no eviction and can tolerate losing its
    state on restart — the opposite of both `live` and `cache`).

    In `local` all five point at one instance with different database
    indices, exactly as the four already did.
    """

    model_config = SettingsConfigDict(env_prefix="REDIS_", frozen=True, extra="forbid")

    live_url: SecretStr = SecretStr(_LOCAL_REDIS_URLS["live"])
    bus_url: SecretStr = SecretStr(_LOCAL_REDIS_URLS["bus"])
    broker_url: SecretStr = SecretStr(_LOCAL_REDIS_URLS["broker"])
    cache_url: SecretStr = SecretStr(_LOCAL_REDIS_URLS["cache"])

    #: Rate limit counters (A64-011.8). Its own instance in a deployed
    #: tier, configured with **no eviction policy** — see this class's
    #: docstring on why sharing `cache` would be a limiter that vanishes
    #: under load.
    limits_url: SecretStr = SecretStr(_LOCAL_REDIS_URLS["limits"])


class AuthSettings(SectionSettings):
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


class JWTSettings(SectionSettings):
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


class SessionSettings(SectionSettings):
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


class EmailSettings(SectionSettings):
    """`email` — outbound mail, email-verification tokens (A64-011.6) and
    password-reset tokens (A64-011.7).

    ## Why 24 hours

    A verification link is a bearer credential that grants exactly one
    thing: marking an address verified. Its window is a trade between two
    real failures — too short and a link sitting in a spam folder
    overnight is dead by morning, too long and a link forwarded, logged by
    a mail gateway or left in a shared inbox stays live for a week.

    Twenty-four hours is the task's figure and is defensible: it survives
    a night and a time zone, and it is short enough that the resend flow
    (rather than a long-lived link) is the answer to "I lost it".

    ## Why the URL is a template rather than a base

    The link a person clicks is a **frontend** route, not this API's —
    `/verify-email?token=...` renders a page that then calls
    `POST /auth/email/verify`. Making the whole shape configurable rather
    than assembling it from a host means the frontend can move that route
    without a backend deploy, and a mobile build can point the same
    template at a deep link.
    """

    model_config = SettingsConfigDict(
        env_prefix="EMAIL_", frozen=True, extra="forbid", populate_by_name=True
    )

    verification_token_ttl_hours: int = Field(default=24, ge=1, le=168)

    #: Independent of `SessionSettings.token_entropy_bytes`, so a
    #: verification token can be lengthened without touching refresh
    #: tokens. Same floor, and for the same DB-24 reason.
    token_entropy_bytes: int = Field(
        default=REFRESH_TOKEN_MIN_ENTROPY_BYTES, ge=REFRESH_TOKEN_MIN_ENTROPY_BYTES
    )

    #: `{token}` is substituted with the raw token. Validated below,
    #: because a template missing the placeholder produces links that
    #: cannot possibly work and does so silently.
    verification_url_template: str = "http://localhost:3000/verify-email?token={token}"

    #: **One hour**, against verification's twenty-four, and the asymmetry
    #: is the decision rather than an inconsistency. Both links sit in the
    #: same inbox and face the same threats, but they are worth very
    #: different amounts to whoever finds one: a stolen verification link
    #: confirms an address its owner was about to confirm anyway, while a
    #: stolen reset link *is* the account.
    #:
    #: An hour survives a slow mail relay and a person who reads the email
    #: on a phone and resets on a laptop. It does not survive a message
    #: sitting unread overnight, which is the intended outcome — the
    #: recovery path for "it expired" is another request to
    #: `/auth/password/forgot`, which is cheap and audited, whereas the
    #: recovery path for "somebody else used it" does not exist.
    #:
    #: The `le=` bound is a security boundary, not a form nicety: raising
    #: this to a day would make a reset link as long-lived as a
    #: verification link while being far more valuable, and that should
    #: take a code change and this paragraph, not an environment variable.
    #: See `app/modules/auth/domain/password_reset.py`.
    password_reset_token_ttl_hours: int = Field(default=1, ge=1, le=24)

    #: Independent of the two above, so a reset token can be lengthened
    #: without touching verification or refresh tokens. Same floor, and for
    #: the same DB-24 reason.
    password_reset_token_entropy_bytes: int = Field(
        default=REFRESH_TOKEN_MIN_ENTROPY_BYTES, ge=REFRESH_TOKEN_MIN_ENTROPY_BYTES
    )

    #: A frontend route, exactly as `verification_url_template` is — the
    #: link opens a page that collects the new password and posts it to
    #: `POST /auth/password/reset`. The token must never be submitted to
    #: this API as a query parameter; see `ResetPasswordRequest`.
    password_reset_url_template: str = "http://localhost:3000/reset-password?token={token}"

    otp_secret: SecretStr = Field(
        default=SecretStr(_LOCAL_OTP_SECRET),
        validation_alias="EMAIL_VERIFICATION_OTP_SECRET",
    )
    """The key the six-digit verification code is stored under —
    `EMAIL_VERIFICATION_OTP_SECRET`. **Server-side only.**

    Aliased past this class's `EMAIL_` prefix so the variable says what it
    is for. `EMAIL_OTP_SECRET` would read as "a secret about email"; this
    one names the flow it belongs to, which matters on a platform that will
    have more than one thing to verify.

    Its own secret rather than `JWT_SECRET_KEY`, and the separation is the
    point (A64-021.5H §6). A key that both signs access tokens and derives
    verification verifiers is a key whose compromise is two breaches, and
    rotating it for one reason invalidates the other. Domain separation
    costs one environment variable.

    Explicitly **not** any of: `RESEND_API_KEY` (a third party holds the
    other half), an access or refresh token (per-session, and rotated), or
    the database password (the thing the verifier is meant to survive the
    theft of).

    Why it matters more than a hash salt: a six-digit code has a million
    possibilities, so an unkeyed digest of one is a table an attacker builds
    in a second. Without this key, a stolen `email_verification_tokens` row
    cannot be inverted at all — see `domain.otp.otp_verifier`.

    Refused in a deployed tier while it is the local default, by the same
    guard that refuses the development JWT key. `SecretStr`, so it cannot
    reach a log or a traceback through a repr.
    """

    from_address: str = "no-reply@arena64.gg"
    """Who transactional mail comes from — `EMAIL_FROM_ADDRESS`.

    `arena64.gg` is the **verified sending domain**, and the default names it
    because it is the only address this platform is entitled to send as. It
    was `no-reply@arena64.local` while no domain existed; a placeholder TLD
    is the right default for a platform that cannot send and the wrong one
    for a platform that can.

    Changing it is not a configuration knob so much as a deliverability
    decision: an address outside a domain with SPF, DKIM and DMARC records
    for Resend is refused by the provider, which this platform records as a
    permanent failure and stops retrying.
    """

    from_name: str = "Arena64"
    """The display name beside the address — `EMAIL_FROM_NAME`."""

    resend_api_key: SecretStr | None = Field(default=None, validation_alias="RESEND_API_KEY")
    """The Resend credential — `RESEND_API_KEY`. **Server-side only.**

    The alias is load-bearing. This class carries an `EMAIL_` prefix, so
    without it the variable would be `EMAIL_RESEND_API_KEY` — and an
    operator following the provider's own documentation would set
    `RESEND_API_KEY`, get no error, and run a deployment that silently
    cannot send. `PUBLIC_APP_URL` carries one for the same reason.

    `SecretStr`, like every credential here, so it cannot reach a log line, a
    traceback or an error reporter through a repr (services.md §8.5).

    `None` means *no transport is configured*, and it is the switch the whole
    channel turns on:

        set    `ResendEmailProvider` is built, and the notification email
               channel reports itself available
        unset  `ConsoleEmailProvider` is built — which refuses to construct
               in a production-like tier, so a deployed process without a key
               fails at boot rather than sending nobody anything (DI-06)

    That is why availability is not a second flag. A boolean saying "email
    works" that could disagree with whether a credential exists is exactly
    the settings-screen lie A64-021.5 §26 forbids.
    """

    @model_validator(mode="after")
    def _url_templates_must_carry_the_token(self) -> "EmailSettings":
        """Both templates, checked together.

        A template missing its placeholder produces links that are all
        identical and none of which works, and it does so *silently* —
        nothing raises, the mail sends, and the failure surfaces as
        "verification is broken in staging" a day later. Checking at
        construction turns it into a process that refuses to start
        (DI-06).
        """
        templates = {
            "EMAIL_VERIFICATION_URL_TEMPLATE": self.verification_url_template,
            "EMAIL_PASSWORD_RESET_URL_TEMPLATE": self.password_reset_url_template,
        }
        for name, template in templates.items():
            if "{token}" not in template:
                raise ValueError(
                    f"{name} must contain '{{token}}' — without it every link "
                    "it generates is identical and none of them works"
                )
        return self

    def verification_url(self, token: str) -> str:
        """The link to put in the message.

        One of only two places the raw token is ever interpolated into
        anything, which is what makes "never log the token" checkable: the
        value exists here, in the message body, and nowhere else on this
        side of the wire.
        """
        return self.verification_url_template.format(token=token)

    def password_reset_url(self, token: str) -> str:
        """The link to put in the reset message. See `verification_url`."""
        return self.password_reset_url_template.format(token=token)


class NotificationEmailSettings(SectionSettings):
    """`notification_email` — the Notification email channel, A64-021.5.

    Separate from `EmailSettings`, which owns the *transport identity* and
    the two credential links `auth` sends. What lives here is the
    **channel**: whether it delivers at all, how a link into the app is
    built, and how a failed send is retried.

    ## `enabled` is a kill switch, not the provider gate

    It was off by default while this platform had chosen no email vendor.
    Resend is now the vendor, and **`RESEND_API_KEY` is the gate**: a process
    with a credential can send and one without cannot, which is a fact rather
    than a flag. `platform.email.can_deliver_email` reads it, and the same
    value decides which provider is built.

    What is left here is an operational switch — a way to stop notification
    email without withdrawing the credential that `auth`'s verification and
    reset mail also depend on. Two knobs answering different questions: *can
    this process send at all*, and *should it send notifications*.

    Defaulting it to `True` is what makes the pair honest. An operator who
    configures Resend expects notification email to work; a default of
    `False` would mean a correctly configured deployment silently sending
    nothing, which is the same surprise pointing the other way.
    """

    model_config = SettingsConfigDict(env_prefix="NOTIFICATION_EMAIL_", frozen=True, extra="forbid")

    enabled: bool = True
    """Whether this process delivers notification **email** — the kill switch.

    Necessary and not sufficient: a process also needs a transport, which is
    `RESEND_API_KEY`. Both are composed into one `ChannelAvailability` at the
    composition root and threaded to every preference read and every refusal
    — see `notifications.presentation.dependencies.email_channel_available`."""

    #: How many deliveries one worker pass claims. Small, because each is a
    #: network call to a provider and a pass that claimed hundreds would
    #: hold them all against one timeout budget.
    batch_size: int = Field(default=20, ge=1, le=200)

    #: How often the worker looks for due deliveries. Email is not
    #: interactive — a minute of latency on a tournament confirmation is
    #: invisible, and polling every second would be a query per second for a
    #: table that is empty most of the time.
    poll_interval_seconds: float = Field(default=30.0, ge=1.0, le=600.0)

    #: Attempts before a delivery is abandoned. Five, spanning roughly seven
    #: hours with the backoff below — long enough to outlast a provider
    #: incident, short enough that a permanently broken address stops being
    #: retried the same day.
    max_attempts: int = Field(default=5, ge=1, le=10)

    #: The first retry delay, doubling each attempt up to the ceiling.
    retry_base_seconds: int = Field(default=60, ge=1)
    retry_max_seconds: int = Field(default=6 * 60 * 60, ge=60)


class PushSettings(SectionSettings):
    """`push` — Web Push identity and delivery, A64-021.6 §5, §18.

    ## The key pair is the switch

    There is no `PUSH_ENABLED`. A process holding a valid VAPID pair can
    send a push notification; one that does not, cannot — and
    `ChannelAvailability` is built from exactly that. A boolean beside the
    keys would be a second source of truth that can disagree with them, and
    the player is the one who finds out: a settings switch that turns on a
    channel with nothing behind it (§6).

    ## Absent is allowed; wrong is not

    Unlike `RESEND_API_KEY`, whose absence makes registration unverifiable,
    an unset pair costs one optional channel. A tier that has not generated
    keys reports push unavailable, refuses to store subscriptions, and says
    so on the settings screen — all true, so there is nothing to fail at
    boot over.

    A *malformed or mismatched* pair does raise at startup, and the
    asymmetry is deliberate: it means somebody intended to configure push
    and got it wrong, and accepting it binds every subscription created
    afterwards to a key that cannot sign for it. Fixing the configuration
    later does not repair those subscriptions.

    ## Why rotation is not routine

    A browser commits to the public key when it subscribes, and a push
    service refuses anything not signed by its private half. Changing the
    pair therefore invalidates **every existing subscription immediately**
    — every browser must subscribe again. That makes this operational state
    rather than a credential on a rotation schedule, and it is why nothing
    in this platform generates a pair at startup.

    Generate one deliberately:

        python -m app.operator.push_keys generate
    """

    model_config = SettingsConfigDict(env_prefix="PUSH_", frozen=True, extra="forbid")

    vapid_public_key: str | None = Field(default=None, validation_alias="VAPID_PUBLIC_KEY")
    """The application server key, base64url, unpadded — `VAPID_PUBLIC_KEY`.

    **Not a secret.** It is handed to every browser that subscribes and is
    served to the frontend, which is why it is a plain `str`: marking it
    `SecretStr` would imply a handling rule that does not exist and would
    make the one value that *must* be published look like one that must not.

    The alias is load-bearing, for the reason `RESEND_API_KEY` documents:
    this class carries a `PUSH_` prefix, so without it an operator following
    any Web Push documentation would set `VAPID_PUBLIC_KEY`, get no error,
    and run a deployment that silently cannot send.
    """

    vapid_private_key: SecretStr | None = Field(default=None, validation_alias="VAPID_PRIVATE_KEY")
    """The signing key, base64url, unpadded — `VAPID_PRIVATE_KEY`.
    **Server-side only.**

    Never reaches a browser, a `VITE_` variable or a response body. Anybody
    holding it can sign an assertion this platform's own subscriptions
    accept, which means they can push to every one of them.

    `SecretStr` so it cannot reach a log line, a traceback or an error
    reporter through a repr (services.md §8.5).
    """

    vapid_subject: str = Field(
        default="mailto:no-reply@arena64.gg", validation_alias="VAPID_SUBJECT"
    )
    """A way for a push service operator to reach whoever is sending —
    `VAPID_SUBJECT`, `mailto:` or `https:` per RFC 8292 §2.1.

    Has a real default rather than `None` because it is not a secret, is the
    same in every tier, and a missing one makes some push services refuse
    the assertion. It is also the one field here safe to log, and is what a
    boot line uses to say which configuration was loaded.
    """

    ttl_seconds: int = Field(default=6 * 60 * 60, ge=0, le=28 * 24 * 60 * 60)
    """How long a push service may hold a message for an offline device.

    Six hours. The message is a *pointer* to a durable notification that is
    already stored, so a device that was off overnight loses nothing by
    missing the push — it sees the notification on next open. What the
    window buys is the laptop closed for an afternoon.

    Not zero, which would drop anything for a sleeping device, and not days,
    which would wake somebody at breakfast about a tournament round that
    finished before they went to bed.
    """

    batch_size: int = Field(default=20, ge=1, le=200)
    """Deliveries claimed per worker pass. Matches the email channel's, and
    for the same reason: a pass holds a database session for its duration,
    and a large batch is a long-lived transaction."""

    poll_interval_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    """How often the worker looks for due deliveries."""

    max_attempts: int = Field(default=5, ge=1, le=10)
    """Attempts before a delivery is abandoned.

    Five, matching email. A push service that has been unreachable across
    five backoffs is not going to take this message, and the notification
    itself is already durable — nothing is lost but the interruption.
    """

    retry_base_seconds: int = Field(default=60, ge=1)
    retry_max_seconds: int = Field(default=6 * 60 * 60, ge=60)


class StorageSettings(SectionSettings):
    """`storage` — where binary objects live (A64-012.2).

    One provider today and the setting that chooses it, because the choice
    is a deployment concern rather than a code one: the same artifact runs
    on a laptop with a directory and in a deployed tier with a bucket
    (architecture.md AD-02's one-artifact rule).

    `LocalStorageProvider` refuses to construct in a production-like
    environment, so a tier that never sets `STORAGE_PROVIDER` fails at
    startup rather than accepting uploads onto a disk that vanishes on the
    next reschedule.
    """

    model_config = SettingsConfigDict(env_prefix="STORAGE_", frozen=True, extra="forbid")

    provider: Literal["local"] = "local"
    """The only value today. A `Literal` rather than a free string so that
    `STORAGE_PROVIDER=s3` on a tier where S3 is not yet implemented fails
    at startup with a readable error, rather than silently falling through
    to local and losing every upload."""

    local_root: str = "var/storage"
    """Where `LocalStorageProvider` writes. Relative to the process working
    directory, and deliberately inside the repository's ignored `var/` —
    development objects are disposable, and putting them under a path a
    developer might commit is how a 5 MB test avatar ends up in git."""

    public_base_url: str = "http://localhost:8000/media"
    """The prefix `get_public_url` composes keys onto.

    Configurable rather than derived, because in a deployed tier it is a
    CDN or bucket host that has nothing to do with this process's own
    address — and because that is precisely the substitution that lets a
    CDN be put in front without any code change.

    In `local` it must match where `app_factory` mounts `StaticFiles`, and
    a validator below enforces the pairing rather than leaving two settings
    to drift into a 404 nobody can explain.
    """

    @property
    def public_url_path(self) -> str:
        """The path component of `public_base_url` — what `StaticFiles` is
        mounted at in development.

        Derived rather than configured separately, so the mount point and
        the generated URL cannot disagree. Two settings for one fact is how
        an avatar renders as a broken image on somebody else's machine.
        """
        path = self.public_base_url.split("://", 1)[-1]
        mount = path[path.index("/") :] if "/" in path else "/media"
        return mount.rstrip("/") or "/media"


# Configured with **class keywords** rather than a `model_config`, and this
# is the only section that is. Seven modules key an `lru_cache` on an instance
# of this class (`presentation/rate_limits.py` in each), which requires it to
# be hashable — which `frozen=True` makes it. A type checker only sees that
# through `dataclass_transform`, and `dataclass_transform` cannot see inside
# `SettingsConfigDict`: spelled there, Pyright rejects all seven call sites as
# unhashable, while mypy's pydantic plugin accepts them. The keywords are
# identical at runtime, visible to both checkers, and pydantic refuses to mix
# the two forms — hence all three move, not just `frozen`.
#
# The suppression is the cost of spelling it this way. `dataclass_transform`
# forbids a frozen class inheriting a non-frozen one, and `SectionSettings`
# declares no `frozen` at all because each section decides for itself.
# Pydantic permits it, and every other section is frozen through its own
# `model_config` — so the rule is describing a dataclass invariant this base
# class does not have.
class RateLimitSettings(
    SectionSettings,
    env_prefix="RATE_LIMIT_",
    frozen=True,  # pyright: ignore[reportGeneralTypeIssues]
    extra="forbid",
):
    """`rate_limit` — abuse prevention on the authentication endpoints
    (A64-011.8), and since A64-012.4 on the privacy settings endpoint too.

    One settings class for the whole platform rather than one per module:
    the *policy* — which rules an endpoint carries — belongs to the module
    that owns the endpoint (`auth.presentation.rate_limits`,
    `profiles.presentation.rate_limits`), but the numbers belong in the one
    file an operator edits during an incident. Two of them would mean
    finding out during the incident which one applied.

    Every limit is configurable rather than constant, for the reason
    `AuthSettings` gives about Argon2's cost: a control that can only be
    changed by a deploy is a control that cannot be tightened during an
    incident. A credential-stuffing run in progress is answered by lowering
    `login_ip_limit` and restarting, in minutes, not by a release.

    The defaults are the figures A64-011.8 specifies. Two are not, and both
    are called out below (`password_reset_ip_limit` and the dimensions
    chosen for the two endpoints whose brief gave a count but no
    dimension) — a chosen default is worth more than a missing one, and
    saying which is which is worth more than either.

    ## The numbers here are **production's**, always

    A64-021.6 added `profile`, which scales every one of them for local
    development and for the end-to-end suite. Nothing below changes: the
    figures are production's, the scaling is a multiplier applied at the
    guard, and `profile` is **derived from `ENVIRONMENT` and cannot be set**
    — see the field for why an operator-settable one would be a way to ship
    hundred-fold limits.

    ## `ge=` floors, and why there are no `le=` ceilings

    The floors exist because a limit of zero takes an endpoint down
    completely (see `RateLimitRule.__post_init__`), and a misconfigured
    environment variable that parses as `0` is a far more likely event than
    a deliberate one.

    There is no upper bound, deliberately. Raising a limit is a
    *loosening*, and an operator who needs to raise one at 3am to keep a
    launch alive should not be blocked by a bound this file guessed at.
    Lowering is the direction that matters and the floors protect it.
    """

    profile: RateLimitProfile = RateLimitProfile.PRODUCTION
    """How hard every limit below bites — A64-021.6.

    **Derived from `ENVIRONMENT`, and overwritten by `get_settings()` after
    this class is constructed.** `RATE_LIMIT_PROFILE` may be present in an
    environment and is *ignored*: an operator-settable profile is a way to
    ship hundred-fold limits to production by editing one variable, which is
    exactly the failure this whole mechanism must not introduce. The default
    is `PRODUCTION` so that a `RateLimitSettings` built by hand — in a test,
    or by a future caller — is the strict one until something deliberately
    relaxes it.

    The scaling is applied at the guard (`api.rate_limiting.RateLimit.rules`)
    rather than to the numbers below, so every figure in this class stays
    production's and every assertion about them stays readable.
    """

    enabled: bool = True
    """The kill switch. `False` disables every rule — for a load test, or
    for the incident where the limiter itself is the problem.

    Present because the alternative to a documented switch is somebody
    commenting out a dependency under pressure and forgetting to restore
    it, which is how an endpoint ends up unprotected for a quarter."""

    fail_open: bool = True
    """What an unreachable or slow Redis means.

    **`True` (default): the request is allowed.** A Redis outage then
    degrades abuse prevention rather than removing the ability to sign in.
    That is the right default for this platform and the reasoning is worth
    stating plainly, because the opposite is defensible elsewhere:

      - Failing closed converts a *rate-limiting* outage into a **total
        authentication outage** — nobody signs in, nobody registers,
        nobody recovers a password, and every logged-in session dies at
        its next refresh. That is a self-inflicted platform outage
        (system-design.md T-2) triggered by the least critical dependency
        in the request path.
      - Rate limiting is not the only control on these endpoints. Argon2id
        at ~20ms still bounds guess throughput, `users.locked_until` still
        exists, sign-in still returns one generic failure, and reset tokens
        still carry 256 bits. Losing the limiter is losing defence in
        depth, not losing the defence.
      - The outage is loud: every failure logs at ERROR with
        `rate_limit_unavailable`, so "we are currently unprotected" is an
        alertable condition rather than a silent one.

    Set to `False` for a tier where credential stuffing is the greater
    risk than availability. The limiter then returns `503` — not `429` —
    because "our dependency is down" is not "you did too much", and a
    client that saw 429 would back off politely for an hour over a fault
    that may clear in seconds."""

    trusted_proxy_count: int = Field(default=0, ge=0)
    """How many reverse proxies sit in front of this process.

    **This is the setting that decides whether per-IP limiting works at
    all**, and both of its wrong values are quietly catastrophic in
    opposite directions, which is why it is explicit rather than inferred.

    `0` (the default) means "no proxy": the caller's IP is the socket peer,
    and `X-Forwarded-For` is ignored entirely. That is the only safe
    default, because a process that trusts `X-Forwarded-For` without a
    proxy in front of it lets **any client set its own rate-limit
    identity** — a header away from unlimited login attempts, which is not
    a limiter with a bug but a limiter with an off switch.

    Set it to the real number of proxies in a deployed tier. Behind a load
    balancer with the default `0`, every request appears to come from the
    balancer, so all traffic on the platform shares one per-IP bucket and
    the first five sign-ins of any fifteen minutes lock out everybody
    else — a total outage in the shape of a working feature.

    The count is used to index from the *right* of the header, which is
    what makes it unspoofable: each trusted proxy appends one entry, so the
    (count+1)-th from the end is the address the outermost trusted proxy
    actually observed. Entries to the left of it may be forged and are
    never read."""

    redis_timeout_ms: int = Field(default=100, ge=1)
    """How long a limit check may take before it is treated as a failure.

    Without this the fail-open policy above is decorative: a Redis that is
    *slow* rather than *down* — the common failure — would hang every
    authentication request for the client's default timeout, and the
    limiter would take the platform down while being perfectly available
    itself.

    100ms is roughly two orders of magnitude above a healthy local round
    trip and one fifth of the Argon2 verification the login path is about
    to perform anyway, so it costs nothing observable when things are well
    and cuts fast when they are not."""

    # --- POST /auth/login ---------------------------------------------------
    # Two rules, and the pair is the point. Per-IP bounds a single attacker
    # brute-forcing one account; per-email bounds a *distributed* attempt on
    # one account, which is exactly what per-IP cannot see. Credential
    # stuffing is the distributed case by definition.
    #
    # **A64-020.6 raised the per-IP limit from 5 to 20** and left the
    # per-email limit alone. The two are not interchangeable, and which one
    # moved matters:
    #
    #   - An **IP is not a user.** A household, an office, a school and every
    #     mobile carrier behind CGNAT share one egress address, so five
    #     attempts per quarter hour is five *people* — the sixth is locked
    #     out of a platform that is working perfectly. That is a denial of
    #     service this file inflicts on its own users, and it scales with how
    #     successful the product is.
    #   - The per-IP rule was never the anti-brute-force control. Guessing
    #     one account's password is bounded by `login_email_limit` (ten an
    #     hour, unchanged, and unevadable by adding hosts), by Argon2id at
    #     ~20ms, and by `users.locked_until`. Twenty per quarter hour is
    #     eighty an hour from one host against *all* accounts, which
    #     credential stuffing needs millions of to be worth running.
    #
    # So this loosens the rule that was punishing shared egress and keeps the
    # rule that actually bounds an attack. Both remain configurable, and
    # lowering either is one environment variable during an incident.
    login_ip_limit: int = Field(default=20, ge=1)
    login_ip_window_seconds: int = Field(default=15 * 60, ge=1)
    login_email_limit: int = Field(default=10, ge=1)
    login_email_window_seconds: int = Field(default=60 * 60, ge=1)

    # --- POST /auth/register ------------------------------------------------
    # Per IP only: there is no account yet, so there is no per-account
    # dimension to count. Bounds mass account creation.
    #
    # **A64-020.6 raised this from 3 to 10 an hour**, for the reason above
    # applied to a harsher case: registration is the *first* thing a new user
    # does, so the failure is invisible — a classroom, a games night or a
    # family signing up together silently stops working at the fourth person,
    # and nobody reports it because they never got an account to report it
    # from.
    #
    # Ten an hour from one address still makes mass account creation
    # uneconomic: a botnet is the only way to create accounts at scale, and a
    # per-IP counter has never been the control for that — email
    # verification is (`users.is_verified`, A64-011.6).
    register_ip_limit: int = Field(default=10, ge=1)
    register_ip_window_seconds: int = Field(default=60 * 60, ge=1)

    # --- POST /auth/password/forgot -----------------------------------------
    # Per email: the victim of this endpoint's abuse is the *inbox*, and a
    # botnet spraying one address from a thousand hosts is stopped by
    # counting the address, not the hosts.
    forgot_password_email_limit: int = Field(default=3, ge=1)
    forgot_password_window_seconds: int = Field(default=60 * 60, ge=1)

    # --- POST /auth/email/resend --------------------------------------------
    # The brief gives "3 requests / hour" without a dimension. Read as per
    # email, because this endpoint is the structural twin of forgot-password
    # — unauthenticated, takes an address, sends mail — and mail-bombing one
    # inbox is the abuse that matters. A per-IP companion rule, which would
    # bound *spraying* many addresses from one host, is a recommendation for
    # A64-011.9 rather than a limit invented here.
    resend_verification_email_limit: int = Field(default=3, ge=1)
    resend_verification_window_seconds: int = Field(default=60 * 60, ge=1)

    # --- POST /auth/email/resend-code ---------------------------------------
    # A64-021.5H. The code resend is *authenticated*, so its primary bound is
    # already the 60-second per-user cooldown in `EmailVerificationService` —
    # this rule answers what that cooldown cannot.
    #
    # The cooldown is per account, and accounts are cheap: `register_ip`
    # permits ten an hour from one host, and ten accounts each resending once
    # a minute is six hundred messages an hour from one connection. That is a
    # sending-reputation problem for `arena64.gg` before it is anything else.
    #
    # Per IP rather than per email because the endpoint takes no address —
    # the session says whose challenge it is — so there is no address to key
    # on. Twenty an hour leaves every one of the ten permitted registrations
    # two resends, which is more than a real person needs and far less than a
    # script wants.
    resend_code_ip_limit: int = Field(default=20, ge=1)
    resend_code_window_seconds: int = Field(default=60 * 60, ge=1)

    # --- POST /auth/refresh -------------------------------------------------
    # The brief gives "30 requests / minute" without a dimension. Read as
    # per IP: the credential is an opaque token, and keying on the token
    # would let an attacker holding N stolen tokens make 30N requests —
    # counting the thing being abused rather than the abuser. See the
    # recommendations on what this costs behind a corporate NAT.
    refresh_ip_limit: int = Field(default=30, ge=1)
    refresh_ip_window_seconds: int = Field(default=60, ge=1)

    # --- POST /auth/password/reset ------------------------------------------
    # **A64-011.8 lists this endpoint but specifies no limit**, so this
    # figure is chosen rather than given, and is flagged as such.
    #
    # Per IP, because the token is the only other thing in the request and a
    # correct one is used once. 10/hour is generous against the legitimate
    # pattern (one reset, occasionally retried after a rejected password)
    # and restrictive against what the endpoint actually risks: it performs
    # an Argon2id hash, so it is the cheapest CPU-amplification primitive in
    # the authentication module. Guessing the token itself is not the threat
    # — that is 256 bits.
    password_reset_ip_limit: int = Field(default=10, ge=1)
    password_reset_window_seconds: int = Field(default=60 * 60, ge=1)

    # --- PATCH /profile/privacy ---------------------------------------------
    # **A64-012.4 required this endpoint to be rate limited and specified no
    # figure**, so the number is chosen rather than given.
    #
    # Per **user** since A64-012.6, which asked for the migration
    # explicitly. A64-012.4 shipped it per IP because `RateLimitScope` had
    # only `IP` and `EMAIL`, neither of which fits an authenticated endpoint
    # carrying no address; A64-012.5 added `USER` for the preferences
    # endpoint, and leaving this one on the inferior dimension afterwards
    # would have meant one settings screen throttling a whole office NAT
    # while the screen beside it did not.
    #
    # 20 per 5 minutes is generous against the legitimate pattern (a person
    # working through a settings screen, toggling five switches and
    # changing their mind) and still bounds what the endpoint actually
    # risks: an unbounded authenticated write to the account row.
    privacy_update_user_limit: int = Field(default=20, ge=1)
    privacy_update_window_seconds: int = Field(default=5 * 60, ge=1)

    # --- PATCH /profile/preferences -----------------------------------------
    # **Per authenticated user**, which A64-012.5 specifies explicitly and
    # which `RateLimitScope.USER` exists to express — see that member on why
    # a proven identity beats a network address on any endpoint behind a
    # token, and `profiles.presentation.rate_limits` on how the principal
    # reaches the limiter.
    #
    # 30 per 5 minutes is chosen rather than given. A settings screen is
    # used in bursts — a player opens it, tries three board themes, changes
    # a timezone and leaves — so the window has to absorb a short run of
    # deliberate changes without a legitimate user ever meeting it. What it
    # bounds is a client stuck in a retry loop writing to the account row,
    # which is the realistic failure on an authenticated endpoint that
    # nobody else can reach.
    #
    # No per-IP companion. Adding one would reintroduce exactly the
    # shared-NAT problem the user scope removes, and there is no attack it
    # would catch that the per-user rule does not: an attacker with N stolen
    # tokens is already N compromised accounts, and rate limiting is not the
    # control for that.
    preferences_update_user_limit: int = Field(default=30, ge=1)
    preferences_update_window_seconds: int = Field(default=5 * 60, ge=1)

    # --- GET /users/search ---------------------------------------------------
    # **Per authenticated user**, which A64-013.1 specifies. The dimension is
    # doing more work here than on the two settings endpoints above: those
    # are writes only the account holder can reach, while this is a *read*
    # whose abuse is enumeration — building a list of who is on the platform,
    # a page at a time.
    #
    # Per-user is what makes that bounded rather than merely inconvenient. A
    # per-IP limit is defeated by a botnet and punishes a shared campus
    # connection; counting the account means an enumerator has to hold as
    # many accounts as they want multiples of this budget, and registration
    # is itself rate limited (3/hour per address).
    #
    # 30 per minute is chosen rather than given, and is set against the
    # legitimate pattern rather than the abusive one: a person typing into a
    # search box with a debounced client issues a handful of requests per
    # name they look up, and a friend-adding session is a few names. Thirty
    # absorbs an undebounced client typing a whole username character by
    # character; it does not absorb a script.
    #
    # The window is deliberately short. A minute-long window with a modest
    # limit refuses a burst quickly and forgives it quickly, which is the
    # right shape for an interactive read — an hour-long window with the
    # same rate would lock a legitimate user out for fifty-nine minutes over
    # one stuck key.
    search_user_limit: int = Field(default=30, ge=1)
    search_window_seconds: int = Field(default=60, ge=1)

    # --- friend requests (A64-013.2) -----------------------------------------
    # Both **per authenticated user**, and the dimension is doing more work
    # than on the settings endpoints: what these bound is *harassment*, not
    # load. FR-1 already stops a second pending request to the same person,
    # so an attacker's remaining move is to spray requests at many people —
    # which only a per-account budget counts. Per-IP would let a botnet
    # spread it and would throttle a campus network for one student.
    #
    # 20 sends per hour is chosen rather than given, and is set against the
    # legitimate pattern: adding the people you know is a burst of a handful
    # in one sitting and then almost nothing. Twenty absorbs a new player
    # working through a search page; it does not absorb a script, and it is
    # low enough that reaching it is itself a signal worth alerting on.
    friend_request_send_user_limit: int = Field(default=20, ge=1)
    friend_request_send_window_seconds: int = Field(default=60 * 60, ge=1)

    # --- POST /challenges (A64-022.2) ----------------------------------------
    # Per **account**, for the reason the friend-request send limit above
    # gives: what this bounds is one person spraying invitations, and a
    # per-IP budget is defeated by a botnet while throttling a shared
    # connection for everybody on it.
    #
    # The structural rules already do most of the work — a challenge can only
    # go to a friend, and only one may be live per pair — so an attacker's
    # remaining move is to challenge *every* friend, which only a per-account
    # counter sees.
    #
    # Twenty an hour, matching friend requests deliberately: both are "invite
    # somebody you know", the legitimate pattern is the same handful in one
    # sitting, and two different numbers for one shape of action would be two
    # numbers to explain rather than one to tune. It is also more than the
    # median friend list, so reaching it means challenging people you have
    # not played — which is the signal.
    challenge_create_user_limit: int = Field(default=20, ge=1)
    challenge_create_window_seconds: int = Field(default=60 * 60, ge=1)

    # Decline and cancel share one counter, like the friend-request responses
    # below. Neither can reach a challenge the caller is not party to, so this
    # bounds a stuck client rather than an attack — sixty in five minutes is
    # far past any human rate and well under a retry loop's.
    challenge_respond_user_limit: int = Field(default=60, ge=1)
    challenge_respond_window_seconds: int = Field(default=5 * 60, ge=1)

    # Accept, decline and cancel share one counter. None can reach a request
    # the caller is not party to, so this bounds a stuck client rather than
    # an attacker — hence the far looser figure and the short window.
    friend_request_respond_user_limit: int = Field(default=60, ge=1)
    friend_request_respond_window_seconds: int = Field(default=5 * 60, ge=1)

    # --- GET /profiles/{username} --------------------------------------------
    # **Per IP**, and it has to be: the endpoint is anonymous by design, so
    # there is no account to count. A64-013.2 asks for this endpoint to be
    # migrated to "the correct rate limiting", and for an unauthenticated
    # read the correct dimension is the only one available.
    #
    # It is the platform's most enumerable surface — a username at a time —
    # and until now it was unlimited, which the last three tasks each flagged
    # as debt. 120 per minute is deliberately generous: a profile page is a
    # normal thing to open repeatedly, and a server-rendered page under AD-24
    # may fetch several per view. What it stops is a scraper walking a
    # dictionary of handles, which needs orders of magnitude more.
    #
    # Behind a proxy this is only meaningful with `trusted_proxy_count` set
    # correctly — see that setting, whose two wrong values are both severe.
    profile_read_ip_limit: int = Field(default=120, ge=1)
    profile_read_window_seconds: int = Field(default=60, ge=1)

    # --- POST /profile/avatar ------------------------------------------------
    # **Per user**, because it is authenticated — the correct dimension for a
    # write behind a token, and the one A64-013.2 asks these endpoints be
    # migrated to.
    #
    # This is the most expensive operation on the platform per call: a decode
    # and two encodes, on bytes a caller supplies. 10 per hour is generous
    # against the legitimate pattern (people change their avatar rarely, and
    # a few times in a row when they are fiddling with it) and is the
    # tightest limit on the platform against the thing it actually risks,
    # which is CPU amplification from an account that costs one registration.
    avatar_upload_user_limit: int = Field(default=10, ge=1)
    avatar_upload_window_seconds: int = Field(default=60 * 60, ge=1)

    # --- matchmaking queue (A64-014.1) ---------------------------------------
    # **Per authenticated user**, one budget shared by joining and leaving.
    # See `matchmaking.presentation.rate_limits` on why the two share a
    # counter and why the read of your own ticket carries none.
    #
    # 30 per 5 minutes is chosen rather than given, and is set against the
    # legitimate pattern: a player queues, waits, gives up, queues in a
    # different pool, plays. That is a handful of calls in a sitting, and
    # thirty absorbs a client that retries a dropped response or a person
    # who cannot decide between ranked and casual.
    #
    # What it bounds is pool churn — repeatedly re-queueing to influence who
    # you are paired with, which is a rating-manipulation vector rather than
    # a load problem. Reaching this limit is itself a signal worth alerting
    # on, which is why it is not looser.
    matchmaking_queue_user_limit: int = Field(default=30, ge=1)
    matchmaking_queue_window_seconds: int = Field(default=5 * 60, ge=1)

    # --- match acceptance (A64-015.4) ----------------------------------------
    # **Per authenticated user**, and a separate budget from the queue's, which
    # is the one place these two endpoint groups differ from the join/leave
    # pair that shares one. The argument for sharing there was that joining
    # and leaving are *one behaviour* — nobody joins two hundred times, they
    # join and leave two hundred times. Accepting is not that behaviour: a
    # player who has spent their queue budget must still be able to answer the
    # match they already have, and a shared counter would mean the platform
    # pairing somebody and then refusing to let them say yes.
    #
    # 20 per 5 minutes is chosen rather than given. A player answers at most
    # one match per pairing and a pairing takes at least the reservation
    # window, so twenty absorbs a client that retries a dropped response
    # several times across several matches. What it bounds is a stuck client
    # hammering an endpoint that takes a row lock on a match — which is the
    # realistic failure on a write only two accounts can reach.
    #
    # `GET /matchmaking/matches/pending` carries no limit, for the reason
    # `GET /matchmaking/queue/me` does not: it is the endpoint a client polls
    # while deciding, and throttling it would make a working handshake look
    # broken in exactly the situation it is working.
    matchmaking_acceptance_user_limit: int = Field(default=20, ge=1)
    matchmaking_acceptance_window_seconds: int = Field(default=5 * 60, ge=1)

    # --- PATCH /notifications/preferences (A64-021.3) -------------------------
    # **Per authenticated user**, for the reason every settings write on this
    # platform is: the endpoint sits behind a token, so the platform knows
    # whose account is being written, and per-IP would make one office or one
    # mobile carrier a single bucket.
    #
    # 30 per 5 minutes matches `preferences_update_user_limit`, and
    # deliberately so — it is the same behaviour on a different screen, and
    # two different numbers for "a person toggling settings" would be two
    # numbers to explain rather than one to tune. A save is one request
    # however many switches moved, so thirty is a person working through the
    # whole matrix twice over, with room for a client that retries.
    #
    # The read carries none, like `GET /profile/privacy`: it is one indexed
    # read of at most a dozen of the caller's own rows, and a caller who
    # repeats it learns their own settings.
    notification_preferences_update_user_limit: int = Field(default=30, ge=1)
    notification_preferences_update_window_seconds: int = Field(default=5 * 60, ge=1)

    # --- POST /notifications/push/subscriptions (A64-021.6) -------------------
    # Per **user**, not per IP: what this bounds is one account accumulating
    # rows, and an office behind one address is many accounts each entitled
    # to their own devices.
    #
    # A client registers on enabling push and again on each app start, so a
    # person who opens the app ten times a day and has three browsers is
    # thirty registrations — all upserts, all of which touch one row each.
    # Sixty an hour absorbs that and still stops a script minting endpoints,
    # which is the only real abuse available here: every row is a capability
    # the delivery worker will POST to.
    #
    # `DELETE` is limited by the same rule, deliberately sharing the bucket.
    # Enable and disable are two halves of one action, and separate counters
    # would let anybody willing to alternate have double the allowance.
    push_subscription_user_limit: int = Field(default=60, ge=1)
    push_subscription_window_seconds: int = Field(default=60 * 60, ge=1)


class StatisticsSettings(SectionSettings):
    """`statistics` — the competitive-record projection (A64-012.6).

    One setting, and it is a kill switch rather than a feature flag.
    """

    model_config = SettingsConfigDict(env_prefix="STATISTICS_", frozen=True, extra="forbid")

    enabled: bool = True
    """Whether profiles read a player's real record.

    **`True` (default): `DatabaseStatisticsProvider` is wired.** Setting it
    to `False` wires `NoMatchesStatisticsProvider` instead, and every
    profile then reports a blank record.

    Present for the same reason `RateLimitSettings.enabled` is: the
    alternative to a documented switch is somebody commenting out a
    dependency under pressure and forgetting to restore it. What it is
    actually for is a store being rebuilt or a store that is unhealthy —
    `player_statistics` is a projection and rebuildable by definition
    (database.md C5), so the sane failure mode is a profile page without
    numbers rather than no profile page at all, which is the platform's
    highest-volume public read (§1436).

    **The degradation is not transparent**, and that is worth stating
    rather than glossing: while this is off, a player with a real record is
    indistinguishable from a brand-new account, on their own profile as
    well as on a stranger's. The composition root logs the choice at
    `WARNING` on every request so "we are currently serving blank
    statistics" is an alertable condition rather than a quiet one.
    """


class PresenceSettings(SectionSettings):
    """`presence` — who is online right now (A64-012.7).

    ## Which Redis role presence uses, and why it is not a sixth one

    `cache`. AD-03 separates roles by *hostile interaction*, and presence's
    profile matches that instance's posture almost exactly: it is derived
    rather than authoritative, it is expendable, it expires on its own, and
    losing it is a cosmetic defect (system-design.md §626 — "a stale 'online'
    indicator"). `cache` is the instance already configured with no
    persistence and an eviction policy, because evicting from it is correct.

    The three it must *not* share are more interesting than the one it does:

      **Not `live`.** AD-03's own worked example. A deploy or a network blip
      produces a reconnect storm, which is a write burst of one key per
      returning player — precisely the traffic that must not compete for
      memory or connections with the positions of games in progress. Presence
      loss is cosmetic; live match state loss interrupts matches (AD-18).

      **Not `limits`.** That instance is deliberately configured to evict
      nothing, because a rate limit counter dropped under memory pressure is
      a limit that disappears during the traffic spike it exists for.
      Presence is exactly the high-churn, safely-evictable workload that
      would put it under that pressure.

      **Not `broker`.** Celery owns the keyspace.

    A dedicated sixth role is the AD-03-consistent answer if presence write
    volume ever becomes large enough to evict the leaderboard read models
    beside it. Not warranted today: since A64-013.6 the writers are
    `POST /auth/login`, `POST /auth/refresh` and `POST /auth/logout-all`, so
    the write rate is bounded by the token refresh interval rather than by
    socket churn. Recorded as a revisit-when — AD-09's gateway is what would
    change the volume — rather than guessed at now.
    """

    model_config = SettingsConfigDict(env_prefix="PRESENCE_", frozen=True, extra="forbid")

    enabled: bool = True
    """Whether presence is read from Redis at all.

    **`True` (default): `RedisPresenceProvider` is wired.** `False` wires
    `NoPresenceProvider`, and every profile then reports `is_online: null`
    and `last_seen: null`.

    Present for the reason `RateLimitSettings.enabled` and
    `StatisticsSettings.enabled` are: the alternative to a documented switch
    is somebody commenting out a dependency under pressure and forgetting to
    restore it. What it is actually for is a presence instance being
    replaced or resized.

    Unlike the statistics switch, **the degradation here is honest.** A
    blank statistics record is indistinguishable from a beginner's and
    therefore misleading; unknown presence is the same `null` a profile
    already reports for a player who is offline or who has hidden it, which
    every client must handle regardless.
    """

    ttl_seconds: int = Field(default=60, ge=5, le=3600)
    """How long an observation stands before the platform stops asserting it.

    **This is the liveness protocol, not a tuning knob.** Nothing tells the
    platform that a gateway node died, so the only thing that stops a dead
    node's players being marked online forever is the record expiring on its
    own. Whatever writes presence must rewrite it well inside this window —
    a refresh interval of roughly a third of it leaves room for two missed
    writes before a present player flickers offline.

    Sixty seconds is chosen rather than given. Shorter makes an online
    indicator flicker for anyone on a mobile network; much longer means a
    player who closed their laptop lid shows as available for minutes, and
    the cost of that is specific on this platform — a challenge sent to a
    player who is not there is a challenge that expires, which is the
    reasoning `PrivacySettings.show_online_status` records for why the flag
    defaults to on.

    The `ge=` floor exists because a TTL below a plausible refresh interval
    is not a shorter window, it is presence that never reports anybody.
    """

    redis_timeout_ms: int = Field(default=50, ge=1)
    """How long a presence operation may take before it is abandoned.

    Without it the "never raises" promise on both ports is decorative: a
    Redis that is *slow* rather than *down* — the common failure — would hang
    every profile read for the client's default timeout, and presence would
    take down the platform's highest-volume public read while being perfectly
    available itself.

    Tighter than the rate limiter's 100ms, and deliberately so. That budget
    sits in front of an Argon2 verification that costs five times as much
    anyway; this one sits on a read whose whole latency budget is a few
    milliseconds, and the thing being protected is an indicator nobody would
    trade a page load for.
    """

    sweep_interval_seconds: float = Field(default=15.0, ge=1.0, le=600.0)
    """How often the presence sweeper looks for lapsed records — A64-013.8.

    This is the **worst-case delay on an `offline` notification** for a
    player who closed the tab: their window closes, and up to one interval
    later the sweeper notices. Fifteen seconds against a sixty-second window
    means a departure is announced within a quarter of the time the record
    was already stale for, which is well inside what "friend went offline"
    is worth.

    Shorter is not free. Each tick is one `ZRANGEBYSCORE` with a limit, so
    the cost is small — but it is a database session and a Redis round trip
    per tick per process, and a one-second sweeper on a quiet platform is
    almost entirely empty ticks.
    """

    sweep_batch_size: int = Field(default=200, ge=1, le=2000)
    """How many lapsed players one sweep may announce.

    Bounded for the reason every batch on this platform is (CLAUDE.md §10.5):
    the interesting case is a *deploy*, where a node restarting takes every
    player it held offline at once. Two hundred per tick drains that in a few
    seconds without one transaction holding thousands of outbox inserts.
    """

    sweeper_enabled: bool = True
    """Whether *this process* runs the sweeper.

    Per-process, exactly like `OUTBOX_WORKER_ENABLED`, and for the same
    deployment shape: one API tier with it off, one worker tier with it on.

    It is a separate switch from `enabled` because they mean different
    things. `PRESENCE_ENABLED=false` makes the roster permanently empty, so
    the sweeper is harmlessly idle either way; this one decides which
    *process* does the work.
    """

    @property
    def ttl_ms(self) -> int:
        """`ttl_seconds` in the unit Redis's `PX` takes.

        Derived rather than configured, so the two cannot disagree — and
        expressed once here rather than as a `* 1000` in the adapter, which
        is the arithmetic somebody eventually writes as `* 100`.
        """
        return self.ttl_seconds * 1000


class FriendsSettings(SectionSettings):
    """`friends` — the social graph (A64-013.3).

    One setting, and it is a kill switch rather than a feature flag — the
    same shape as `StatisticsSettings.enabled` and `PresenceSettings.enabled`.
    """

    model_config = SettingsConfigDict(env_prefix="FRIENDS_", frozen=True, extra="forbid")

    enabled: bool = True
    """Whether profile composition consults the social graph.

    **`True` (default): `FriendshipRelationshipProvider` is wired**, so a
    viewer who is a friend sees fields set to `friends`. `False` wires
    `NoRelationshipsProvider`, and every viewer is treated as a stranger.

    Present for the reason the other two kill switches are: the alternative
    to a documented switch is somebody commenting out a dependency under
    pressure and forgetting to restore it. What it is actually for is a
    `friends.friendship` relation being migrated, or a social graph that is
    unhealthy on a read path that runs for every profile render.

    **The degradation narrows rather than widens.** With the graph off, a
    field restricted to friends is hidden from everyone — a visible loss of
    functionality, not a disclosure. That is the only acceptable direction
    for a privacy control to fail in, and it is why this switch is safe to
    reach for during an incident.

    It does **not** disable the friend-request or friend-list endpoints.
    Those are writes and reads of the relation itself; this governs only
    whether *profile composition* consults it.
    """

    cache_enabled: bool = True
    """Whether the social graph is read through the `friends:v1:` cache —
    A64-013.6.

    **`True` (default): `RedisSocialGraphCache` is wired.** `False` wires
    `NoSocialGraphCache`, and every read goes to PostgreSQL — which is
    exactly what the platform did before this task, so the fallback is a
    legitimate degradation rather than a stub.

    Separate from `enabled` above, deliberately. That one turns the social
    graph *off*, which changes what players see; this one turns the cache
    off, which changes nothing a player can observe and only costs queries.
    Collapsing them would mean an operator with a misbehaving cache had to
    choose between it and friends-only visibility.
    """

    cache_ttl_seconds: int = Field(default=300, ge=10, le=3600)
    """How long a cached friend or block set survives without being
    invalidated.

    **A backstop, not the mechanism.** Invalidation is exhaustive — four
    triggers, all of them methods on services in `friends` — so a correct
    system never depends on this expiring. What it bounds is how long a bug
    in one of those four triggers, or a `DEL` that failed against an
    unreachable Redis, can serve a removed friend or a lifted block.

    Five minutes is chosen against that failure rather than against a hit
    rate: long enough that the cache does its job on any realistic session,
    short enough that a stale block is measured in minutes rather than
    hours. Lowering it is the right first move if invalidation is ever
    suspected; raising it past an hour would make a missed trigger an
    incident rather than a blip, which is why the bound exists.
    """

    cache_timeout_ms: int = Field(default=50, ge=1)
    """How long a cache operation may take before it is abandoned.

    Without it the "never raises" promise on every method of
    `RedisSocialGraphCache` is decorative: a Redis that is *slow* rather
    than down — the common failure — would hang every profile render for the
    driver's default, and the cache would take down the read it exists to
    accelerate.

    Matched to `PresenceSettings.redis_timeout_ms`, because both sit on the
    same hot path with the same budget: a few milliseconds, on a read whose
    fallback is a query that costs about the same.
    """


class OutboxSettings(SectionSettings):
    """`outbox` — the transactional event log and its relay (A64-013.7).

    AD-16 makes the outbox non-negotiable, so unlike presence or the social
    graph cache there is **no setting here that turns correctness off**.
    `enabled` exists and is an emergency switch, not a feature flag: with it
    off, state changes still commit and their consequences stop being
    recorded, which loses information rather than degrading performance. It
    is documented as such in `.env.example` and logs at `WARNING` per event.

    Everything else is the relay's operating envelope, and the defaults are
    chosen for the deployment this build actually has — one API process,
    one in-process worker, a social event rate measured in events per
    minute rather than per second.
    """

    model_config = SettingsConfigDict(env_prefix="OUTBOX_", frozen=True, extra="forbid")

    enabled: bool = True
    """Whether domain events are made durable at all. See the class docstring
    on why this is an emergency switch rather than a feature flag."""

    worker_enabled: bool = True
    """Whether *this process* runs the relay loop.

    Per-process rather than per-deployment, which is the whole point: the
    intended shape is one API tier with this off and one worker tier with it
    on, running the same image. It defaults to `true` so that a single-node
    development environment delivers events without a second process — the
    configuration a contributor has, not the one a production tier has.
    """

    poll_interval_seconds: float = Field(default=1.0, ge=0.05, le=60.0)
    """How long the relay sleeps between ticks when there is nothing to do.

    system-design.md §801 budgets the relay at "low seconds", because delay
    here delays every downstream effect on the platform. One second is well
    inside that and costs one indexed query per second against an index that
    is empty when the relay is healthy (§12.5).

    The floor of 50ms is not a tuning range — it is a guard against a
    configuration that turns the relay into a busy loop against PostgreSQL.
    """

    batch_size: int = Field(default=50, ge=1, le=500)
    """How many entries one tick claims.

    Bounded on both sides for different reasons. Too small and a backlog
    drains at `batch_size / poll_interval` events per second, which is a
    ceiling somebody discovers during an incident. Too large and one tick
    holds a claim — and a handler's I/O — over hundreds of rows, which turns
    a slow consumer into a long transaction (CLAUDE.md §10.5: bound
    everything unbounded).
    """

    max_attempts: int = Field(default=5, ge=1, le=20)
    """How many times an entry may be claimed before it stops being claimed.

    With the default backoff this is roughly eighty seconds of retrying,
    after which the row stays unpublished and shows up in the backlog metric
    — see `OutboxEntry` on why an exhausted event must stay visible rather
    than move to a dead-letter table nobody watches.
    """

    retry_base_seconds: int = Field(default=5, ge=1, le=300)
    """The first retry delay. Doubles per attempt, capped below."""

    retry_max_seconds: int = Field(default=300, ge=1, le=3600)
    """The backoff ceiling.

    Five minutes rather than an hour: every consumer on this platform today
    delivers a *social* notification, and one that arrives an hour after the
    fact is worse than one that never arrives — it is a notification about
    somebody who came online and has since gone.
    """

    # --- retention (A64-014.1) ----------------------------------------------
    # The bound A64-013.7 shipped without. See
    # `app/platform/outbox/retention.py` for what a horizon buys and costs;
    # these are the numbers, and they are settings rather than constants
    # because a platform that discovers it needs ninety days should raise a
    # value, not write a migration.

    cooldown_audit_retention_hours: int = Field(default=2160, ge=1, le=8760)
    """How long a **cooldown audit row** is kept — A64-015.6 §3.

    Ninety days, and far longer than the one-hour horizon on the bar itself.
    The asymmetry is the point rather than an inconsistency: the enforcement
    row answers "may this player queue right now" and is worthless the moment
    it lifts, while the audit row answers "why could I not queue last month",
    which is when somebody actually asks.

    Ninety days is the shortest window that covers a complaint routed through
    support, escalated, and looked at — and it is short enough that this
    stays a queue-delay log rather than becoming a behavioural profile. A
    validator keeps it strictly above `cooldown_retention_hours`: an audit
    trail pruned before the thing it explains answers nothing.
    """

    timeline_retention_hours: int = Field(default=336, ge=1, le=8760)
    """How long a **reconciliation timeline entry** is kept — A64-015.6 §4.

    Fourteen days, matched to `OUTBOX_RETENTION_DAYS`, and the match is
    deliberate: the timeline is a *projection* of `pairing_reconciled`, and
    AD-19 makes a projection something that can be rebuilt from its source.
    Keeping it longer than the outbox would leave rows nothing could
    reconstruct; keeping it shorter would throw away an answer the source
    still holds.

    Fourteen days is also the right length for what it is for. "Why did my
    ticket go back in the queue" is an operational question asked within a
    shift or two, and the counter beside it
    (`matchmaking.reconciliation_actions_total`) is what carries the long-run
    trend.
    """

    retention_enabled: bool = True
    """Whether *this process* prunes the outbox.

    Per-process, exactly like `worker_enabled` and
    `PresenceSettings.sweeper_enabled`, and for the same deployment shape:
    one API tier with it off, one maintenance tier with it on, running the
    same image. Setting it to `false` everywhere is how an operator stops
    deletion during an investigation — the table then grows, loudly and
    visibly, which is the correct direction for a switch that governs
    destruction.
    """

    retention_days: int = Field(default=14, ge=1, le=3650)
    """How long a **published** entry is kept, measured on `occurred_at`.

    Fourteen days is chosen rather than given, and it is set against what
    the log is actually used for. An operator asking "why did this player
    not get that notification" is asking about something that happened
    within a shift or two; a projection rebuild does not read this table at
    all (AD-19 requires every projection to be rebuildable from PostgreSQL,
    which is what let AD-17 give up stream replay).

    What it is *not* set against is disk. Two weeks of the current event
    rate is negligible, and the number would be the same at ten times the
    volume — the reason for a horizon is that "no horizon" is not a policy,
    not that fourteen days is the affordable one.

    An unpublished entry is never pruned, whatever this says.
    """

    ledger_retention_days: int = Field(default=30, ge=1, le=3650)
    """How long a `processed_event` row is kept.

    **At or beyond `retention_days`**, and `RetentionPolicy` refuses to
    construct otherwise. It is an ordering invariant rather than a
    preference: dropping a ledger row while its outbox entry can still be
    claimed lets that entry be redelivered *and* re-handled, which is the
    double effect the ledger exists to prevent.

    Thirty against fourteen leaves a fortnight of margin, so a clock skew,
    a paused pruner or a raised `retention_days` cannot invert the pair
    between two deploys.
    """

    prune_interval_seconds: float = Field(default=3600.0, ge=60.0, le=86400.0)
    """How often a prune runs.

    Hourly. Retention is a *floor*, not a deadline — nothing is wrong if a
    row survives an extra hour past its horizon — so the interval is chosen
    to keep each run small rather than to keep the horizon sharp. The floor
    of a minute is a guard against a configuration that turns a `DELETE`
    loop into a busy one.
    """

    prune_batch_size: int = Field(default=1000, ge=1, le=10000)
    """Rows per `DELETE`. Bounds the lock one statement takes on the
    platform's highest-churn relation."""

    prune_max_batches: int = Field(default=20, ge=1, le=1000)
    """Batches per run. Bounds the job.

    The two together cap one run at 20,000 rows per relation, which is
    ~5.5 hours of drain per day at the hourly interval — far above any
    plausible steady-state rate, and low enough that the *first* run after
    this ships does not try to delete a year of history in one job.
    """


class MatchmakingSettings(SectionSettings):
    """`matchmaking` — the queue domain (A64-014.1), the pairing scan
    (A64-015.3), and the acceptance handshake (A64-015.4).

    Three groups. The first governs how long a player may wait before the
    platform stops asserting they are waiting, and which process notices.
    The second governs how a scan decides who plays whom — QT-5's widening
    rating window, and how much of a pool one pass reads. The third governs
    the window between "you have been paired" and "you are playing", and
    who cleans up when nobody answers.
    """

    model_config = SettingsConfigDict(env_prefix="MATCHMAKING_", frozen=True, extra="forbid")

    ticket_ttl_seconds: int = Field(default=600, ge=30, le=86400)
    """How long a queue ticket stays `waiting` before it expires.

    **Ten minutes**, chosen rather than given, and set against what a
    stale ticket costs. A ticket outliving the player's attention is worse
    than a short one: the first thing A64-014.2's pairing worker will do
    with a waiting ticket is create a match, and a match created for
    somebody who left ten minutes ago is a game the opponent has to sit
    through the join deadline of.

    Shorter would make a player who queued and walked to the kitchen have
    to re-queue, which is the ordinary case this must not punish. The two
    together put the number in minutes rather than in seconds or hours.

    It is deliberately **not** coupled to `PRESENCE_TTL_SECONDS`. A closed
    tab is a presence question and is answered by that window; this one is
    about attention, and a player watching a queue spinner is present the
    whole time.
    """

    expiry_enabled: bool = True
    """Whether *this process* expires due tickets.

    Per-process, exactly like `OUTBOX_WORKER_ENABLED` and
    `PRESENCE_SWEEPER_ENABLED` — one API tier with it off, one worker tier
    with it on, running the same image.

    With it off everywhere, `expires_at` still governs what a player sees:
    `active_ticket` treats a due ticket as absent, so a stale row cannot
    block a re-queue. What stops happening is the *transition* — no
    `expired` status, no event, no log line — which is a loss of the record
    rather than of the rule.
    """

    expiry_interval_seconds: float = Field(default=15.0, ge=1.0, le=600.0)
    """How often due tickets are swept.

    This is the worst-case delay between a ticket falling due and the
    platform recording it as expired. Fifteen seconds against a ten-minute
    window is a rounding error on the ticket's life, and matches
    `PRESENCE_SWEEP_INTERVAL_SECONDS` — the two are the same kind of job
    and there is no reason for an operator to hold two numbers.
    """

    expiry_batch_size: int = Field(default=200, ge=1, le=2000)
    """How many tickets one sweep may expire.

    Bounded for the reason every batch on this platform is (CLAUDE.md
    §10.5). The interesting case is a queue that filled while the sweeper
    was down: two hundred per tick drains it in seconds without one
    transaction holding hundreds of row locks and as many outbox inserts.
    """

    snapshot_limit: int = Field(default=200, ge=1, le=1000)
    """How many waiting tickets one queue snapshot reads.

    The snapshot is the read A64-014.2's pairing scan will run, so it is
    bounded from the first release rather than when a pool first gets
    large. Two hundred is well past the point where a pairing pass has
    found a match, and the depth reported beside it is a count over the
    same predicate rather than the length of this page — so a bounded read
    never turns into a wrong number.
    """

    pairing_enabled: bool = True
    """Whether *this process* scans pools for pairings — A64-015.3.

    **On since A64-015.4.** It shipped `False` for one task, because `game`
    had no match persistence: every pairing a scan found would have been
    reserved, refused, and released, several times a second forever. That
    was the compensation path working exactly as designed, and it was still
    churn for no match.

    The five things A64-015.4 §12 required before this could flip are all
    true, and each is a thing rather than an assertion:

        durable match creation   `PersistentMatchCreation` over `game.match`
        pairing_id uniqueness    `uq_match__pairing_id`
        ticket settlement        `PairingService._complete`, unchanged
        reconciliation wired     `MATCHMAKING_RECONCILIATION_ENABLED`
        acceptance timeout       `reservation_ttl_seconds`, below

    Per-process, like `expiry_enabled` and `OUTBOX_WORKER_ENABLED` — one
    API tier with it off, one worker tier with it on, running the same
    image. Turning it off everywhere stops new pairings and leaves the rest
    of the queue working: players still join, wait, and expire.
    """

    pairing_interval_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    """How often one pool is scanned.

    A second, against fifteen for the expiry sweep, and the asymmetry is
    the point: an expiry that is fifteen seconds late is invisible against
    a ten-minute window, while a pairing that is fifteen seconds late is
    fifteen seconds a player spends watching a spinner beside somebody they
    could already be playing. architecture.md §14 puts pairing's freshness
    target at ~1s for exactly that reason — "perceived quality of
    matchmaking is mostly perceived speed".
    """

    candidate_batch_size: int = Field(default=200, ge=2, le=1000)
    """How many waiting tickets one pairing pass reads from a pool.

    Bounded like every batch on this platform (CLAUDE.md §10.5), and the
    bound is what keeps the scan's cost independent of how popular the
    platform becomes: it reads the *oldest* two hundred, which is where a
    queue that serves the longest wait first is always going to find its
    answer.

    It is also the width of the block read — `blocked_pairs_among` takes
    exactly these players — so raising it lengthens one query's parameter
    list rather than adding queries.

    Two is the floor because one candidate cannot be a pair.
    """

    rating_window_initial: int = Field(default=100, ge=0, le=5000)
    """The rating gap a fresh ticket accepts — QT-5's starting window.

    A hundred points is roughly "the same class of player" on every rating
    scale this platform might adopt, and it is deliberately narrow: the
    window widens on its own, so starting tight costs a few seconds and
    starting loose costs a bad first game.
    """

    rating_window_widen_every_seconds: float = Field(default=15.0, ge=1.0, le=600.0)
    """How long a ticket waits before its window takes one step outward."""

    rating_window_widen_by: int = Field(default=50, ge=0, le=5000)
    """How many points one step adds."""

    rating_window_maximum: int = Field(default=600, ge=0, le=10000)
    """The widest gap a pairing may ever bridge.

    Reached after about two and a half minutes at the defaults, which is
    well inside the ten-minute ticket window — so a player in a thin pool
    spends most of their wait at the maximum rather than approaching it,
    and then expires honestly rather than being handed a hopeless game.
    """

    reservation_ttl_seconds: int = Field(default=30, ge=5, le=300)
    """How long a claimed pairing may stand before it is reconciled, and
    how long a player has to accept it — A64-015.4 §5.

    **One number for both**, which is what §5 means by "model reservation
    and acceptance timeout coherently instead of creating two unrelated
    timers". `PairingService._claim` computes `now + this` once, writes it
    to both reserved tickets as `reserved_until`, and sends the same instant
    to `game` as the match's `acceptance_deadline`. There is exactly one
    arithmetic expression on this platform that produces the window, and
    every row that carries it carries the same value.

    Thirty seconds is chosen rather than given, and it is set against the
    only thing it is really about: **how long the opponent stares at a
    spinner while somebody decides**. A player who has been waiting for a
    match answers in a second or two; thirty absorbs a phone waking up, a
    page still loading, and a distracted glance away, and it is short enough
    that a player whose opponent walked off is back in the queue before they
    have thought about leaving.

    The floor of five seconds is a guard rather than a range: below a
    plausible round trip this is not a tighter window, it is a handshake
    that expires before the offer arrives.

    A validator below keeps it strictly under `ticket_ttl_seconds` — §5
    requires "shorter than the normal queue-ticket lifetime", and a
    reservation that could outlive its own ticket would leave the two
    deadlines racing.
    """

    reconciliation_enabled: bool = True
    """Whether *this process* recovers stranded pairings — A64-015.4 §9.

    **On by default, and this one is closer to a correctness switch than a
    kill switch.** With it off everywhere, a worker that dies mid-pairing
    leaves two tickets `reserved` until their ordinary ten-minute window
    closes and the expiry sweep takes them — so the two players wait out a
    queue that stopped considering them, and a match that was created but
    not settled stays that way. Nothing is corrupted; recovery simply stops
    being automatic, which is the state A64-015.3 shipped in.

    It is a switch at all for the reason `OUTBOX_RETENTION_ENABLED` is one:
    an operator investigating an incident needs to be able to stop a job
    that rewrites rows, and the alternative to a documented switch is
    somebody commenting out a scheduler under pressure.

    Per-process, like every other job flag on this platform.
    """

    reconciliation_interval_seconds: float = Field(default=5.0, ge=1.0, le=600.0)
    """How often stranded pairings are looked for.

    Five seconds, between the pairing scan's one and the expiry sweep's
    fifteen, and it is set by what is *waiting* on it: a match whose
    acceptance window closed is two players who need to be told, and a
    reservation with no match is a player standing in a queue that cannot
    see them. Both are measured against a thirty-second window, so five
    seconds is a sixth of the time anybody could be affected.

    Cheaper than it looks. A healthy platform's tick is two indexed reads
    against partial indexes that are empty — `ix_queue_ticket__stale_reservation`
    and `ix_match__pending_deadline` both carry only rows currently in
    flight — which is the same property that makes `ix_outbox__unpublished`
    a direct measure of relay health.
    """

    decline_cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    """How long an explicit decline bars a player from the queue —
    A64-015.5 §3.

    **Sixty seconds**, chosen rather than given, and set against the thing
    it exists to stop: a client — or a person — cycling the queue until it
    produces an opponent they like the look of. One minute makes that cost
    more than it is worth without being a punishment: a player who declined
    because they genuinely had to leave has already left, and one who
    misclicked waits about as long as it takes to notice.

    It applies to a **decline** and to nothing else. A player whose window
    closed without an answer earns no cooldown at all — §3 forbids treating
    silence as a decline, and `CooldownReason` has one member so that stays
    structural rather than remembered.

    **Zero disables it**, which is what the `ge=0` floor is for. That is a
    kill switch rather than a tuning value: with it off, declining is free
    and the queue-churn vector is bounded only by
    `RATE_LIMIT_MATCHMAKING_QUEUE_USER_LIMIT`. `MatchOutcomeService` records
    no cooldown at all in that case rather than a zero-length one, so no
    row is written and no `409` is ever raised.
    """

    ticket_retention_hours: int = Field(default=72, ge=1, le=8760)
    """How long a **terminal** queue ticket is kept — A64-015.5 §8.

    Three days, and it is set by the one question the row answers after the
    fact: *why was I matched with them?* The inputs to that answer are
    `entered_at`, the pool and the rating snapshot, and it is asked by
    support the same day or the next. Three days covers a Friday-evening
    complaint read on Monday morning.

    A64-014.1 shipped this relation with no horizon at all and said so:
    "resolved tickets accumulate … storage grows with matches attempted,
    forever". This is the number that closes it.

    The floor is a guard rather than a range. Below the reservation TTL and
    the ticket TTL the horizon would start deleting rows the reconciler is
    about to read — and while the retention *predicate* makes that
    impossible (a live ticket is unreachable from the delete), a horizon
    measured in minutes would still remove the audit trail of every pairing
    within the hour. A validator below keeps it clear of both.
    """

    abandoned_match_retention_hours: int = Field(default=168, ge=1, le=8760)
    """How long a **cancelled or expired** match is kept — A64-015.5 §8.

    Seven days, longer than the ticket horizon, and the asymmetry is the
    decision: "why was I matched with them" is answered from a ticket and
    asked within a day, while "why did my opponent decline" is answered
    from a match and is where a support conversation starts a week later.

    A match that was **played** is not covered by this or by any horizon.
    A64-015.4 recorded why — it is the permanent competitive record A-4 is
    about, and DM-13's anonymise-don't-delete position exists so that it
    survives erasure — and `AbandonedMatchRetention` excludes `active` by
    predicate rather than by configuration, so no value here can reach one.
    """

    cooldown_retention_hours: int = Field(default=1, ge=0, le=168)
    """How long a **lapsed** cooldown row is kept past its own expiry.

    One hour, and short by design: a cooldown that has lifted answers no
    question anybody will ask, unlike a resolved ticket or a settled match.
    It is not zero only so that a read in flight when the row expires does
    not race the delete — and the read's answer is the same either way, so
    the margin buys nothing except the absence of a confusing failure.
    """

    cooldown_audit_retention_hours: int = Field(default=2160, ge=1, le=8760)
    """How long a **cooldown audit row** is kept — A64-015.6 §3.

    Ninety days, and far longer than the one-hour horizon on the bar itself.
    The asymmetry is the point rather than an inconsistency: the enforcement
    row answers "may this player queue right now" and is worthless the moment
    it lifts, while the audit row answers "why could I not queue last month",
    which is when somebody actually asks.

    Ninety days is the shortest window that covers a complaint routed through
    support, escalated, and looked at — and it is short enough that this
    stays a queue-delay log rather than becoming a behavioural profile. A
    validator keeps it strictly above `cooldown_retention_hours`: an audit
    trail pruned before the thing it explains answers nothing.
    """

    timeline_retention_hours: int = Field(default=336, ge=1, le=8760)
    """How long a **reconciliation timeline entry** is kept — A64-015.6 §4.

    Fourteen days, matched to `OUTBOX_RETENTION_DAYS`, and the match is
    deliberate: the timeline is a *projection* of `pairing_reconciled`, and
    AD-19 makes a projection something that can be rebuilt from its source.
    Keeping it longer than the outbox would leave rows nothing could
    reconstruct; keeping it shorter would throw away an answer the source
    still holds.

    Fourteen days is also the right length for what it is for. "Why did my
    ticket go back in the queue" is an operational question asked within a
    shift or two, and the counter beside it
    (`matchmaking.reconciliation_actions_total`) is what carries the long-run
    trend.
    """

    retention_enabled: bool = True
    """Whether *this process* prunes queue history — A64-015.5 §8.

    Per-process, exactly like `OUTBOX_RETENTION_ENABLED`, and for the same
    deployment shape: one API tier with it off, one maintenance tier with it
    on, running the same image. Setting it to `false` everywhere is how an
    operator stops deletion during an investigation — the relations then
    grow, loudly and visibly, which is the correct direction for a switch
    that governs destruction.
    """

    retention_interval_seconds: float = Field(default=3600.0, ge=60.0, le=86400.0)
    """How often retention runs.

    Hourly. A horizon is a **floor, not a deadline** — nothing is wrong if
    a row survives an extra hour past it — so the interval is chosen to keep
    each run small rather than to keep the horizon sharp. The same reasoning
    `OUTBOX_PRUNE_INTERVAL_SECONDS` records, and the same floor: a minute is
    a guard against a configuration that turns a `DELETE` loop into a busy
    one.
    """

    retention_batch_size: int = Field(default=500, ge=1, le=10000)
    """Rows per statement. Bounds the lock one delete takes."""

    retention_max_batches: int = Field(default=20, ge=1, le=1000)
    """Batches per relation per run. Bounds the whole job.

    The two together cap one run at 10,000 rows per relation, which is far
    above any plausible steady-state rate and low enough that the **first**
    run after this ships does not try to delete the platform's whole queue
    history in one job.
    """

    realtime_delivery_enabled: bool = True
    """Whether *this process* pushes pending matches to connected players —
    A64-015.5 §4.

    **On by default**, and the degradation with it off is honest rather than
    silent: `GET /matchmaking/matches/pending` still answers, so a polling
    client is unaffected and a pushing one falls back to it (§5). What stops
    is the push, which costs a player up to one poll interval of their
    thirty-second window.

    It is a switch at all because the sink is the newest seam on the
    platform and the first one that will be replaced by a real transport
    (AD-09). An operator whose gateway is misbehaving needs to be able to
    stop feeding it without stopping matchmaking.
    """

    reconciliation_batch_size: int = Field(default=100, ge=1, le=1000)
    """How many stranded reservations, and how many overdue matches, one
    pass may resolve.

    Bounded for the reason every batch on this platform is (CLAUDE.md
    §10.5). The interesting case is a *deploy*: a rolling restart that
    kills workers mid-pairing strands a burst of reservations at once, and
    a hundred per tick drains that in seconds without one transaction
    holding hundreds of row locks and as many outbox inserts.
    """

    challenge_expiry_enabled: bool = True
    """Whether *this process* expires overdue friend challenges — A64-022.6.

    Per-process, exactly like `expiry_enabled` above and for the same
    reason: one API tier with it off, one worker tier with it on, running
    the same image.

    With it off everywhere, `expires_at` still governs what a player sees —
    the list reads exclude an overdue row and `_require_answerable` refuses
    to act on one, so no challenge becomes answerable that should not be.
    What stops happening is the *transition*: no `expired` status, no event,
    no record. That is a loss of history rather than of the rule, which is
    exactly the trade the queue's own switch makes.
    """

    challenge_expiry_interval_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)
    """How often overdue challenges are swept.

    The worst-case delay between a challenge falling due and the platform
    recording it. **A minute, not fifteen seconds**, and the difference from
    the queue's cadence is the window each job serves: a queue ticket lives
    ten minutes and a late expiry is a player staring at a dead spinner; a
    challenge lives twenty-four hours and a minute is four thousandths of
    one percent of its life.

    Nothing waits on this. The recipient already cannot answer an overdue
    challenge and already cannot see it, so the sweep is writing the record,
    not enforcing the rule.
    """

    challenge_expiry_batch_size: int = Field(default=200, ge=1, le=2000)
    """How many challenges one sweep may expire.

    Bounded for the reason every batch on this platform is (CLAUDE.md
    §10.5). The interesting case is a sweeper that was off for a day: two
    hundred per tick drains the backlog in a few ticks without one
    transaction holding hundreds of row locks and as many outbox inserts.
    """

    @model_validator(mode="after")
    def _rating_window_widens(self) -> "MatchmakingSettings":
        """The maximum cannot be below the starting width.

        `RatingWindowPolicy` refuses to construct in that shape, and
        without this the refusal would arrive when the first pairing task
        ran rather than when the process started — DI-06's argument, that
        configuration must fail at startup and not in a background job at
        three in the morning.
        """
        if self.rating_window_maximum < self.rating_window_initial:
            raise ValueError(
                "MATCHMAKING_RATING_WINDOW_MAXIMUM cannot be below "
                "MATCHMAKING_RATING_WINDOW_INITIAL"
            )
        return self

    @model_validator(mode="after")
    def _retention_outlives_the_queue(self) -> "MatchmakingSettings":
        """A64-015.5 §8: retention must not delete what recovery still
        reads.

        The retention *predicate* already makes a live ticket unreachable
        (`resolved_at IS NOT NULL`), so this is not what stops the job
        deleting somebody out of a queue — that is held by the schema. What
        this stops is subtler and would be discovered much later: a horizon
        shorter than the ticket's own lifetime would delete a ticket's audit
        trail while its player could still be *in* the pool it describes,
        and "why was I matched with them" would have no answer for the
        matches that just happened.

        Checked at startup (DI-06) rather than in a review, because a
        retention rule that is wrong is discovered when the data is gone.
        """
        horizon_seconds = self.ticket_retention_hours * 3600
        if horizon_seconds <= self.ticket_ttl_seconds:
            raise ValueError(
                "MATCHMAKING_TICKET_RETENTION_HOURS must exceed "
                "MATCHMAKING_TICKET_TTL_SECONDS — a horizon shorter than a "
                "ticket's own lifetime deletes the history of matches that "
                "are still being played out"
            )
        return self

    @model_validator(mode="after")
    def _reservation_is_shorter_than_the_ticket(self) -> "MatchmakingSettings":
        """A64-015.4 §5: the reservation deadline is shorter than the queue
        ticket's own lifetime.

        Not a preference. A reservation that could outlive its ticket would
        put the two deadlines in a race the reconciler has to arbitrate —
        release a ticket that has already expired, or expire one whose
        match is about to be created — and the arbitration would depend on
        which sweeper ran first. Refusing the configuration at startup
        (DI-06) is cheaper than making that decision correct.
        """
        if self.reservation_ttl_seconds >= self.ticket_ttl_seconds:
            raise ValueError(
                "MATCHMAKING_RESERVATION_TTL_SECONDS must be shorter than "
                "MATCHMAKING_TICKET_TTL_SECONDS — a reservation that can outlive "
                "the ticket it holds leaves the two deadlines racing"
            )
        return self


class GameSettings(SectionSettings):
    """`game` — live play (A64-016.3).

    One number today. It exists because AD-18 puts the in-flight position in
    Redis, and anything in Redis without a horizon is an outage waiting for
    enough traffic (CLAUDE.md §10.5).
    """

    model_config = SettingsConfigDict(env_prefix="GAME_", frozen=True, extra="forbid")

    clock_enabled: bool = True
    """Whether the clock worker runs — A64-016.5 §6, AD-21.

    `False` stops adjudication and nothing else: moves still charge time,
    the move log still records it, and no match ever flags. That is the
    honest degradation for a switch on a *worker* rather than on a feature,
    and it exists because the alternative to a documented switch is somebody
    stopping a process and forgetting which one.

    Untimed matches are unaffected either way — they have no deadline.
    """

    clock_interval_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    """How often expired deadlines are claimed.

    **The resolution of the flag**, not a tuning knob: a player whose time
    runs out is told so within this interval, and on a bullet game an
    interval of ten seconds would let them keep moving for nine of them.

    One second is one `ZRANGEBYSCORE` per worker per second against an index
    that is empty when nothing is expiring — the same cost and the same
    argument as `OUTBOX_POLL_INTERVAL_SECONDS`.
    """

    clock_batch_size: int = Field(default=100, ge=1, le=1000)
    """How many deadlines one pass claims. Bounds the transaction count of a
    single pass, which matters on the tick after an outage when every
    deadline that lapsed meanwhile is due at once."""

    live_state_ttl_seconds: int = Field(default=14400, ge=300, le=604800)
    """How long a match's live position survives without a move.

    **Four hours**, and it is measured against an abandoned game rather
    than a played one. A draughts game is minutes to tens of minutes; four
    hours is well past any plausible think time and short enough that a
    match nobody returned to stops occupying memory the same day.

    Reset on every move, so an active game never expires — the horizon
    applies to silence, not to duration.

    **This is the one TTL on the platform whose expiry loses something that
    cannot be rebuilt.** The durable move log AD-18 pairs with this store
    does not exist yet, so a lapsed key is a game that cannot be replayed.
    The floor of five minutes is a guard against a value that would drop
    positions mid-game; the real fix is the move log, and it is recorded as
    A64-016.3's headline gap rather than papered over with a longer number.
    """


class TournamentSettings(SectionSettings):
    """`tournament` — SPEC-TOURNAMENT §6e (A64-019.5H).

    The no-show policy's two numbers. They are settings rather than
    constants because both are *product* judgements about how long a
    tournament waits for a player, and an operator running a rapid event
    will want a different answer from one running a weekend open — which is
    the test AD-19 applies to decide what deserves a knob.
    """

    model_config = SettingsConfigDict(env_prefix="TOURNAMENT_", frozen=True, extra="forbid")

    no_show_seconds: int = Field(default=300, ge=30, le=3600)
    """How long a tournament match waits for its two players to turn up.

    **Five minutes.** Long enough that somebody who stepped away between
    rounds is not eliminated for it, and short enough that a bracket does
    not stall for an hour on one absentee — a tournament that cannot finish
    is the failure §6c's rematch bound exists to prevent, and a no-show is
    the other way to reach it.

    Stored **per attempt** when the match is created, not read at
    adjudication time: a deploy that lengthens this must not retroactively
    reprieve a player whose deadline already passed, and one that shortens
    it must not eliminate somebody who was inside the window they were
    given.

    The floor of thirty seconds is a guard against a value that would
    adjudicate matches faster than a client can connect.
    """

    no_show_interval_seconds: float = Field(default=30.0, ge=5.0, le=600.0)
    """How often lapsed deadlines are claimed.

    **The resolution of the adjudication**, not a tuning knob: a no-show is
    decided within this interval of its deadline, and a round that is
    waiting on one is waiting this much longer than it has to.

    Well below `no_show_seconds`, and the relationship is checked below —
    a sweep that ran less often than the deadline it enforces would make
    the deadline advisory.
    """

    no_show_batch_size: int = Field(default=100, ge=1, le=1000)
    """How many lapsed attempts one pass claims.

    Bounds the transaction count of a single pass, which matters on the
    tick after an outage when every deadline that lapsed meanwhile is due
    at once.
    """

    @model_validator(mode="after")
    def _sweep_faster_than_the_deadline(self) -> "TournamentSettings":
        """A sweep slower than the window it enforces is not enforcing it.

        Checked rather than documented, for the reason
        `MatchmakingSettings` checks its own pair: two numbers that must
        stay in a relationship are two numbers somebody will eventually set
        independently.
        """
        if self.no_show_interval_seconds >= self.no_show_seconds:
            raise ValueError(
                "TOURNAMENT_NO_SHOW_INTERVAL_SECONDS must be shorter than "
                "TOURNAMENT_NO_SHOW_SECONDS, or a deadline is enforced no "
                "sooner than one sweep after it lapses"
            )
        return self


class BrowserSessionSettings(SectionSettings):
    """`browser_session` — the SPA's refresh cookie and its CSRF policy
    (A64-020.2).

    ## Why a cookie at all, when the API already returns a refresh token

    `POST /auth/login` hands back both credentials in the body, which is
    right for a native client and wrong for a browser: JavaScript that can
    read a thirty-day credential is JavaScript that can leak it, and every
    place a browser can *store* one — `localStorage`, `sessionStorage`, a
    readable cookie — is readable by any script that reaches the page.

    So the browser surface returns the access token in the body (short,
    held in memory, never persisted) and puts the refresh token in an
    `HttpOnly` cookie the page cannot read. The JSON endpoints are
    unchanged and remain the contract for everything that is not a browser.

    ## Why the path is narrow

    `path` scopes which requests carry the cookie. Scoped to the browser
    auth prefix, it is absent from every other API call — so an ordinary
    request cannot be made to act on the session by an attacker who can
    cause a request but not read a response, and the credential is not
    sprayed across every log a proxy keeps.

    ## `SameSite=Lax` is necessary and not sufficient

    Lax stops a cross-site `POST` from carrying the cookie in every current
    browser, which is most of CSRF. It is not sufficient because it is a
    *browser* guarantee: an old or non-conforming client simply does not
    apply it. `trusted_origins` is the server-side half — see
    `presentation/browser_csrf.py`.
    """

    model_config = SettingsConfigDict(env_prefix="BROWSER_SESSION_", frozen=True, extra="forbid")

    cookie_name: str = Field(default="arena64_refresh", min_length=1, max_length=64)

    cookie_path: str = Field(default="/api/v1/auth/browser", min_length=1)
    """Every browser-session endpoint lives under this prefix, and nothing
    else does.

    One explicit path rather than one per endpoint: `logout-all` needs the
    cookie cleared and `refresh` needs it sent, and a cookie written at one
    path cannot be deleted at another — a mismatch here leaves an
    undeletable credential in the jar, which is the worst of both designs.
    """

    cookie_secure: bool | None = None
    """`None` means "decide from the environment" — `False` in `local` and
    `test`, `True` everywhere else.

    Not a plain `True` default, because a developer on `http://localhost`
    would then never receive the cookie at all and would debug a login that
    silently does not persist. Not a plain `False`, because that is the
    setting that must never reach production. Resolved by
    `secure_for(environment)` below so the decision is one function rather
    than a value someone has to remember to override.
    """

    same_site: Literal["lax", "strict", "none"] = "lax"
    """`lax`, deliberately.

    `strict` would drop the cookie on any cross-site navigation *into* the
    app — following a verification link from a mail client is exactly that —
    so a user arriving from their inbox would appear signed out. `none`
    would carry it on genuine cross-site requests, which is the CSRF
    exposure this is here to close.
    """

    trusted_origins: tuple[str, ...] = ()
    """Origins allowed to make cookie-authenticated browser calls.

    Empty in `local` and `test`, where the app is same-origin through the
    Vite proxy and there is no cross-origin case to allow. Required in a
    deployed tier — see the validator on `Settings`.
    """

    def secure_for(self, environment: Environment) -> bool:
        """Whether to mark the cookie `Secure`.

        Explicit configuration wins; otherwise it follows the environment.
        A deployed tier is always `Secure`, and there is deliberately no
        way to configure it off there: a refresh cookie on plaintext HTTP
        is a credential handed to anybody on the path.
        """
        if self.cookie_secure is not None:
            return self.cookie_secure
        return not (environment.is_local or environment.is_test)


class GatewaySettings(SectionSettings):
    """`gateway` — the realtime WebSocket transport (A64-016.1, AD-09).

    Four numbers, and every one of them is a *liveness* parameter rather
    than a tuning knob. Nothing tells this platform that a browser tab was
    closed abruptly, that a phone went through a tunnel, or that a gateway
    node was killed mid-deploy: every one of those looks identical to a
    quiet connection. So the only thing standing between a dead socket and
    a player who is online forever is a bound that expires on its own, and
    these are those bounds.

    They are also **not independent**, which is why they live together and
    are validated together below. `heartbeat_timeout_seconds` must exceed
    the interval clients actually ping at, `connection_ttl_seconds` must
    exceed the heartbeat timeout, and both must sit inside
    `PRESENCE_TTL_SECONDS` — a connection the registry still believes in,
    attached to a presence record that has already lapsed, is a player the
    platform reports as offline while holding their socket open.
    """

    model_config = SettingsConfigDict(env_prefix="GATEWAY_", frozen=True, extra="forbid")

    ticket_ttl_seconds: int = Field(default=30, ge=5, le=300)
    """How long a WebSocket ticket may be redeemed for — AD-09.

    **Thirty seconds**, and the decision is the whole point of the ticket
    existing. AD-09's reasoning is that a browser cannot set headers on a
    WebSocket handshake, so the credential lands in the query string and
    therefore in load balancer logs, proxy logs and browser history. A
    ticket that is valid for seconds and redeemable once makes that leakage
    worthless: by the time anybody reads the log line, the value in it has
    both expired and been spent.

    Thirty is the window between "the client called `POST /auth/ws-ticket`"
    and "the socket is open", which is one round trip plus a TLS handshake
    on a bad mobile connection. The floor of five seconds is a guard rather
    than a range — below a plausible round trip this is not a tighter
    window, it is a ticket that expires before it can be presented.
    """

    connection_ttl_seconds: int = Field(default=90, ge=15, le=600)
    """How long the registry believes in a connection without a heartbeat.

    This is what makes a **crashed gateway node** self-healing. Its
    connections are entries in a sorted set scored by expiry, so a node
    that dies stops refreshing and its entries fall out of every count on
    the next operation by any other node — rather than pinning its players
    online until somebody notices.

    It must exceed `heartbeat_timeout_seconds`, because a connection the
    server is still willing to wait for must not have already been dropped
    from the registry by its own node.
    """

    heartbeat_timeout_seconds: float = Field(default=45.0, ge=5.0, le=300.0)
    """How long a connection may say nothing before it is closed.

    A **receive deadline**, not a poll: the read is `wait_for(receive(),
    this)`, so an idle connection costs nothing until the deadline actually
    lapses. A polling loop at this frequency across 40,000 sockets would be
    40,000 wakeups per interval spent discovering that nothing happened.

    Roughly half of `connection_ttl_seconds` deliberately, which leaves a
    client one missed heartbeat before the server gives up on it.
    """

    max_frame_bytes: int = Field(default=8 * 1024, ge=512, le=1024 * 1024)
    """The largest frame this gateway will decode.

    Eight kilobytes, against a protocol whose largest legitimate message
    today is a few hundred bytes. The bound exists because parsing is the
    first thing an unauthenticated-shaped attacker reaches: without it, one
    socket can make the server allocate and parse whatever it is willing to
    send, which is a memory amplification rather than a protocol error.

    Generous relative to today's messages on purpose — A64-016.2's move
    frames are still small, and a limit that has to be raised for every new
    message type is a limit somebody eventually raises without thinking.
    """

    node_id: str | None = None
    """Which gateway process this is — A64-016.2 §3.

    **Set it in any deployment with more than one gateway replica.** With
    nothing configured the process draws a random identifier once at
    startup, which is *correct* for the connection registry — the identity
    that matters there is "this process instance", and a restart genuinely
    is a different one — and is illegible: a route resolving to `d4f1a2b8`
    says the connection is elsewhere just as well as `gateway-3` does, but
    only the second can be found on a dashboard.

    So the fallback keeps local development working with no configuration
    and loses nothing but legibility. See `app/gateway/node.py`, which also
    holds the length bound and the one forbidden character — the value is
    written into every connection record in `gwconn:v2:`, so its length is
    multiplied by the number of live sockets and its spelling has to survive
    being parsed back out.

    Never reaches a client (§3): no message type carries it, and
    `GatewayMessage` has no field it could land in. Never a metric label
    either (§11) — one series per node is a cardinality that grows with the
    fleet.
    """

    room_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    """How long a room membership stands without being renewed —
    A64-016.2 §8's "empty room expires after TTL".

    **An hour, and it is measured against a game rather than a heartbeat.**
    Every other bound in this class is a liveness parameter of seconds; this
    one is the outer limit on how long a match's routing scope may sit
    around after everyone stopped talking to it, and a draughts game is
    played in minutes to tens of minutes.

    Much longer than `connection_ttl_seconds` on purpose. A member is
    removed when its connection closes — explicitly on `room.leave`, and by
    `GameRoomService.detach` on disconnect — so the TTL is not the primary
    mechanism, it is the backstop for a node that died between the two. A
    short one would evict a live room whose players are thinking; a much
    longer one would keep a stale one past any plausible game.
    """

    move_rate_limit_enabled: bool = True
    """Whether `game.move.submit` is rate limited — A64-016.3 §13.

    Move submission is the first expensive per-frame operation on this
    platform: a database read, a position load, a legal-move generation and
    a Redis compare-and-set. Everything before it is bounded by what a
    socket can physically send; this is not.

    `False` wires `UnlimitedMoves`, which is production code rather than a
    double — the same argument `PRESENCE_ENABLED` makes. It exists because
    the alternative to a documented switch is somebody commenting out a
    dependency under pressure and forgetting to restore it, and turning it
    off means an unbounded move rate.
    """

    move_rate_limit: int = Field(default=30, ge=1, le=600)
    move_rate_limit_window_seconds: int = Field(default=10, ge=1, le=300)
    """How many moves one **connection** may submit per window.

    Thirty in ten seconds, and it is set against a bullet game rather than a
    correspondence one: three moves a second is faster than anybody plays
    and slower than a loop, which is the gap a limit has to sit in. Bursty
    on purpose — the window is short enough that a legitimate flurry near a
    time scramble passes and a sustained flood does not.

    **Per connection, not per player** (`RateLimitScope.CONNECTION`). A
    player with two tabs is two clients, and a shared bucket would let one
    tab's misbehaving loop throttle the other's game — which on a live board
    is a player losing to somebody else's bug.

    A violation refuses the frame and **keeps the connection open** (§13).
    """

    quick_message_rate_limit_enabled: bool = True
    """Whether `game.quick_message.send` is rate limited — A64-023.1 §6.

    `False` wires `UnlimitedQuickMessages`, which is production code rather
    than a double — the same argument `move_rate_limit_enabled` makes.
    Turning it off means an unbounded quick-message rate, which is the one
    way this feature can be used to harass somebody: the catalogue makes
    every individual message harmless, and **repetition** is the abuse the
    limits exist against.
    """

    quick_message_burst_limit: int = Field(default=3, ge=1, le=60)
    quick_message_burst_window_seconds: int = Field(default=10, ge=1, le=60)
    """How many quick messages one **connection** may send in a burst.

    Three in ten seconds: what a player pressing a couple of buttons during
    a scramble looks like, and what a loop does not. Per connection rather
    than per player, for the reason `move_rate_limit` is — a player with two
    tabs is two clients.

    Spent together with the sustained rule in one atomic acquisition, so the
    burst bucket is never charged for a send the sustained rule then
    refuses. See `app/gateway/quick_message_limits.py`.
    """

    quick_message_rate_limit: int = Field(default=6, ge=1, le=240)
    quick_message_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    """How many quick messages one connection may send per sustained window.

    Six a minute, which is a whole game's worth of courtesy — "good luck",
    a couple of "nice move", "good game", "thanks" — inside sixty seconds.
    The burst rule alone would permit one message every three seconds
    indefinitely, which is somebody typing at their opponent for an hour;
    this is the rule that bounds the hour.

    Deliberately **not** shared with the move budget. A player who spams
    quick messages must not consume the allowance their moves need, because
    the punishment for being annoying would then be losing on time — a
    social channel must never be able to starve the gameplay one.

    A violation refuses the frame and **keeps the connection open**, the
    posture every other gateway limit takes.
    """

    bus_max_stream_length: int = Field(default=1024, ge=16, le=100_000)
    """How many entries one node's cross-node stream holds — §9.

    The bound that makes a node which has gone away safe: its stream stops
    growing and the oldest entries are dropped, which for realtime frames is
    the correct loss — a client that missed a ply resynchronises, and one
    that missed the *newest* ply is looking at a stale board anyway.
    """

    bus_stream_ttl_seconds: int = Field(default=300, ge=30, le=86400)
    """How long a node's stream survives without a publish.

    The half the length cap does not give: a node that never comes back
    leaves a capped-but-permanent key, and one key per node that ever
    existed is unbounded in the fleet's *history* rather than its size.
    Refreshed on every publish, so a live node's stream never lapses.
    """

    event_buffer_length: int = Field(default=64, ge=4, le=4096)
    """How many recent events a match's replay buffer keeps — A64-016.6 §3.

    Sixty-four plies is most of a draughts game, so a client that dropped
    for a minute gets incremental events rather than a snapshot. The bound
    exists because a long game must not accumulate frames forever, and the
    cost of it being too small is a full snapshot — a fallback that already
    exists, which is why this can be generous rather than exact.
    """

    event_buffer_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    """How long a match's replay buffer survives without an event.

    An hour, measured against a game rather than a heartbeat — the same
    argument `GATEWAY_ROOM_TTL_SECONDS` makes. It bounds a *finished* match:
    a capped buffer is still one key per match ever played, which is
    unbounded in history rather than in size.
    """

    match_offer_push_enabled: bool = Field(default=True)
    """Whether a pairing is pushed to the paired players' sockets —
    A64-020.5D §2, §10.

    **On by default**, because the alternative is a lobby that polls while
    a socket sits open beside it. Off falls back to
    `LoggingPendingMatchSink`, which is a *diagnostic* mode rather than a
    fallback: nothing degrades silently, and the composition root logs a
    `WARNING` naming the switch.

    A switch rather than an assumption for the same reason
    `forwarding_enabled` is one — an operator investigating a delivery
    problem needs to be able to take the transport out of the picture
    without taking the platform out with it. Correctness does not depend on
    it either way: `GET /matchmaking/matches/pending` is the durable answer
    (§3).
    """

    forwarding_enabled: bool = Field(default=True)
    """Whether this node drains its cross-node bus stream — A64-016.8.

    A switch rather than an assumption, like the clock's, and the same
    shape: with it off a multi-node deployment publishes frames nothing
    reads, which is precisely the state A64-016.5 shipped in. It exists so
    a single-node deployment can turn off a tick that has nothing to do,
    and so an operator can stop a node draining while they investigate one.
    """

    forwarding_interval_seconds: float = Field(default=0.25, ge=0.01, le=10.0)
    """How often a node reads its own bus stream.

    A quarter second, and the number is a **latency budget** rather than a
    tuning knob: it is the worst-case delay this design adds to a move that
    has to cross a node boundary, on top of the network. Small enough to sit
    inside the round trip a player already accepts, large enough that an
    idle fleet is not spending a Redis read per node per ten milliseconds.

    See `app/gateway/forwarding.py` on why this is a poll rather than a
    blocking `XREADGROUP`.
    """

    forwarding_batch_size: int = Field(default=256, ge=1, le=4096)
    """How many bus entries one pass takes.

    Bounded so a node returning from a pause drains at a rate its sockets
    can absorb: an unbounded read would hand one pass a backlog of every
    frame published while it was away, and write all of them before
    yielding.
    """

    spectator_ttl_seconds: int = Field(default=900, ge=60, le=86400)
    """How long one spectator subscription survives — A64-016.7 §3, §6.

    The backstop for a viewer whose socket died without a `spectator.leave`
    and whose node died without running cleanup. Fifteen minutes rather than
    the connection TTL, because the two bound different things: a connection
    TTL asks "is this socket alive", and this asks "should this subscription
    outlive the node that made it" — and the cost of it being too long is a
    fan-out to a connection that no longer exists, which the delivery loop
    already tolerates and counts.

    Refreshed on every rejoin rather than on every heartbeat, which is
    deliberate: a spectator that watches for longer than this and is dropped
    presses watch again, and a heartbeat that refreshed every subscription
    would put a write on the hot path for a key whose loss costs nothing.
    """

    move_idempotency_ttl_seconds: int = Field(default=60, ge=5, le=3600)
    """How long one `(connection, request_id)` answer is remembered — §7.

    The window a client retry actually needs is a client timeout, so a
    minute is generous. Longer keeps answers for retries nobody will send;
    shorter lets a slow retry through as a fresh submission, which the ply
    compare-and-set then refuses as `stale_state` — safe, and confusing.

    Bounded rather than unbounded because §7 forbids "a permanent unbounded
    request cache", and because the keyspace is one entry per move per
    connection.
    """

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> "GatewaySettings":
        """The three timers must nest, and a misordering is silent.

        Checked here rather than left to a comment because every one of
        these failures produces a *working* gateway with a subtly wrong
        liveness model — sockets that flap, or players who are reported
        offline while connected — and the symptom appears under load, days
        later, on somebody else's dashboard.
        """
        if self.connection_ttl_seconds <= self.heartbeat_timeout_seconds:
            raise ValueError(
                "GATEWAY_CONNECTION_TTL_SECONDS must exceed "
                "GATEWAY_HEARTBEAT_TIMEOUT_SECONDS — a connection the server is "
                "still waiting on must not already have left the registry"
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
    email: EmailSettings
    notification_email: NotificationEmailSettings
    push: PushSettings
    storage: StorageSettings
    rate_limit: RateLimitSettings
    statistics: StatisticsSettings
    presence: PresenceSettings
    friends: FriendsSettings
    outbox: OutboxSettings
    matchmaking: MatchmakingSettings
    gateway: GatewaySettings
    game: GameSettings
    tournament: TournamentSettings
    browser_session: BrowserSessionSettings

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
            "REDIS_LIMITS_URL": (self.redis.limits_url, _LOCAL_REDIS_URLS["limits"]),
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

        # A64-021.5. The origin every transactional email links to. A
        # deployed tier on the localhost default sends verification links,
        # password resets and tournament confirmations that point at a
        # machine the recipient does not have — and nothing about it fails
        # visibly: the mail sends and the links are simply dead.
        if self.app.public_url == _LOCAL_PUBLIC_APP_URL:
            raise ValueError(
                f"PUBLIC_APP_URL must be set explicitly in {self.environment} "
                "— every email link would point at localhost"
            )

        # The most consequential of the three. A deployed tier running on
        # the development signing key does not merely leak — it lets
        # anyone holding this repository mint a valid token for any
        # account on the platform, because the key is right there in the
        # source. Unlike a wrong database URL, nothing about it fails
        # visibly: the service starts, serves traffic, and is silently
        # unauthenticated.
        # A64-021.5H. The key a six-digit code is stored under. A deployed
        # tier on the development value means anybody holding this
        # repository can compute the verifier for any code, for any
        # account — which turns a million-value secret into a lookup.
        if self.email.otp_secret.get_secret_value() == _LOCAL_OTP_SECRET:
            raise ValueError(
                f"EMAIL_VERIFICATION_OTP_SECRET must be set explicitly in {self.environment} "
                "— the development value is in the repository"
            )

        if self.jwt.secret_key.get_secret_value() == _LOCAL_JWT_SECRET_KEY:
            raise ValueError(
                f"JWT_SECRET_KEY must be set explicitly in {self.environment} "
                "— refusing the development default, which is published in the "
                "repository and would let anyone forge tokens for any account"
            )

        # A64-020.2. Last, so it cannot mask the three above — each of those
        # is a credential or a datastore pointed at the wrong place, and
        # this one is a defence-in-depth layer whose absence is serious but
        # less immediate.
        if not self.browser_session.trusted_origins:
            raise ValueError(
                "BROWSER_SESSION_TRUSTED_ORIGINS must list the web origins allowed to "
                f"use the browser refresh cookie in {self.environment} — an empty list "
                "in a deployed tier disables the server-side half of the CSRF defence "
                "(browser_csrf.py), leaving only the browser's SameSite guarantee"
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
        email=EmailSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        notification_email=NotificationEmailSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        push=PushSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        storage=StorageSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        # **The profile is forced, never read.** A64-021.6: the environment
        # is the authority, so whatever `RATE_LIMIT_PROFILE` may say in the
        # process environment is discarded here — see the field's docstring
        # on why an operator-settable one would be a way to ship
        # hundred-fold limits.
        rate_limit=RateLimitSettings(_env_file=env_file).model_copy(  # pyright: ignore[reportCallIssue]
            update={"profile": rate_limit_profile_for(environment)}
        ),
        statistics=StatisticsSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        presence=PresenceSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        friends=FriendsSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        outbox=OutboxSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        matchmaking=MatchmakingSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        gateway=GatewaySettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        game=GameSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        tournament=TournamentSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        browser_session=BrowserSessionSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
    )
