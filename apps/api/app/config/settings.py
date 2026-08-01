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
    "limits": "redis://localhost:6379/4",
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


class EmailSettings(BaseSettings):
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

    model_config = SettingsConfigDict(env_prefix="EMAIL_", frozen=True, extra="forbid")

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

    from_address: str = "no-reply@arena64.local"
    from_name: str = "Arena64"

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


class StorageSettings(BaseSettings):
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


class RateLimitSettings(BaseSettings):
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

    model_config = SettingsConfigDict(env_prefix="RATE_LIMIT_", frozen=True, extra="forbid")

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
    login_ip_limit: int = Field(default=5, ge=1)
    login_ip_window_seconds: int = Field(default=15 * 60, ge=1)
    login_email_limit: int = Field(default=10, ge=1)
    login_email_window_seconds: int = Field(default=60 * 60, ge=1)

    # --- POST /auth/register ------------------------------------------------
    # Per IP only: there is no account yet, so there is no per-account
    # dimension to count. Bounds mass account creation.
    register_ip_limit: int = Field(default=3, ge=1)
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


class StatisticsSettings(BaseSettings):
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


class PresenceSettings(BaseSettings):
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
    beside it. It is not warranted today — nothing writes presence until
    AD-09's gateway exists — and adding an instance nobody needs would be
    speculative infrastructure. Recorded as a revisit-when rather than
    guessed at now.
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

    @property
    def ttl_ms(self) -> int:
        """`ttl_seconds` in the unit Redis's `PX` takes.

        Derived rather than configured, so the two cannot disagree — and
        expressed once here rather than as a `* 1000` in the adapter, which
        is the arithmetic somebody eventually writes as `* 100`.
        """
        return self.ttl_seconds * 1000


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
    storage: StorageSettings
    rate_limit: RateLimitSettings
    statistics: StatisticsSettings
    presence: PresenceSettings

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
        email=EmailSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        storage=StorageSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        rate_limit=RateLimitSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        statistics=StatisticsSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
        presence=PresenceSettings(_env_file=env_file),  # pyright: ignore[reportCallIssue]
    )
