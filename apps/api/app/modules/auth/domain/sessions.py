"""The `UserSession` entity — one authenticated context of use.

domain-model.md §6.2 names this aggregate and its purpose: "a browser, a
phone — that can be listed and individually revoked". That sentence is
the whole design brief. Everything here exists to make *listed* and
*individually revoked* possible, which is why the refresh token is stored
as a row rather than being self-contained the way an access token is.

Framework-free by rule (architecture.md §8): no SQLAlchemy, no FastAPI,
no clock. Every method that needs "now" takes it as a parameter, per
AD-07 — which is what makes "this session expires in 30 days" a test that
runs in microseconds rather than one that cannot be written at all.

## Why a session is an entity and not a value

It has identity that survives change: `last_used_at` moves on every use,
`revoked_at` is set once, and the row is still the same session
throughout. DM-01's test exactly.

## The three ways a session stops working

    revoked      someone decided — a player, a password change, a
                 suspension, or reuse detection. Permanent and recorded.
    expired      the absolute 30-day window elapsed
    idle         unused for longer than the idle window

They are separate because they mean different things to the person
holding the token, and because only the first is a security event worth
alerting on. database.md §4.4 requires the first two to coexist; the
third is `last_used_at` doing double duty rather than a fourth column.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from app.core.identifiers import generate_uuid7


class RevocationReason(StrEnum):
    """Why a session was revoked.

    Not decoration, and not merely an audit nicety: database.md §14.3
    requires reuse detection to revoke "with reason `reuse_detected`", and
    domain-model.md §6.2's lifecycle distinguishes `RevokedByPlayer` from
    `RevokedBySystem` because the two need different treatment — one is a
    normal action, the other is the platform acting on the player's
    behalf and is the one worth paging on.

    The task's field list does not include this column. It is added
    because §14.3 makes it load-bearing rather than informational: a
    revocation list where every entry says only "revoked at 14:02" cannot
    answer the one question an incident asks, which is *why*.
    """

    PLAYER = "player"
    """The player signed this device out. Ordinary; not a security event."""

    PASSWORD_CHANGE = "password_change"
    """SE-1 — a password change revokes every session except the one
    performing it. The entire point of changing a password after a
    compromise."""

    SUSPENSION = "suspension"
    """SE-3 — a suspension that lets an existing socket keep playing is
    not a suspension."""

    REUSE_DETECTED = "reuse_detected"
    """A refresh token was presented that had already been rotated away.

    The one reason that is unambiguously an attack. §14.3: the whole
    family is revoked, not just the presented link, "because the attacker
    and the legitimate user now both hold links in the same chain, and
    there is no way to tell which one is presenting". The cost is that a
    legitimate user is signed out; the alternative is leaving an attacker
    with a working credential.
    """

    EXPIRED = "expired"
    """Swept by a future cleanup job rather than by a live request.

    Distinct from simply *being* expired: expiry is evaluated on read
    (`is_expired_at`), so a session past its window is already refused
    without anything having been written. This reason exists for the job
    that eventually tidies the rows, and is never set on the request path.
    """


@dataclass(frozen=True, slots=True)
class SessionDevice:
    """What the player will see in a session list — SE-2.

    A value object rather than three loose parameters, because the three
    are only ever meaningful together and because SE-2 is explicit about
    why they exist: "without this the revocation list is a row of
    identical entries and the player cannot tell which one is the
    attacker." A list showing "Chrome on macOS · Tashkent · 2 minutes
    ago" is actionable; a list of five UUIDs is not.

    Every field is optional. A session created by a client that sends no
    `User-Agent` is still a session, and refusing to create one because a
    cosmetic label is missing would turn a presentational gap into a
    sign-in failure.

    **All three fields are Personal data** (database.md §14.1): they are
    minimised, retention-bounded, and in scope for erasure. `ip_address`
    in particular is stored because SE-2 asks for "originating region" and
    is the only field that answers "was this me?" — not because it is
    needed for anything on the request path.
    """

    device_name: str | None = None
    """Player-visible label, e.g. "Chrome on macOS". Derived from the
    user agent by whatever creates the session — this module does not
    parse user agents, because a user-agent parser is a dependency with a
    monthly update cadence and no place in a domain layer."""

    user_agent: str | None = None
    ip_address: str | None = None


@dataclass(slots=True)
class UserSession:
    """One device's authenticated session.

    Mutable, unlike the value object above: `last_used_at` changes on
    every refresh and `revoked_at` is set once, and neither changes which
    session this is.

    **The raw refresh token is not a field here and never will be.** Only
    `refresh_token_hash` is stored (database.md §14.3: "the token itself
    exists only in transit and in the client"). An entity that could hold
    the plaintext is an entity that will eventually be logged with it.
    """

    id: UUID
    user_id: UUID
    """Opaque. This module never joins to it and never reads a profile
    through it — DM-06's rule that an identifier is the only thing that
    crosses a context boundary."""

    refresh_token_hash: bytes = field(repr=False)
    """SHA-256 of the token. `repr=False` because a hash is not a password
    but it is the thing a stolen backup would be matched against, and a
    dataclass repr lands in tracebacks and error reporters
    (services.md §8.5)."""

    token_family: UUID
    """The rotation chain this session belongs to — database.md §14.3's
    `chain_id`, under the name the task gives it.

    Every session rotated from this one inherits the same value, so reuse
    detection can revoke *the chain* rather than the single presented
    link. A session created by a fresh sign-in starts its own family, and
    for that session `token_family == id`.
    """

    created_at: datetime
    expires_at: datetime
    """Absolute expiry — the 30-day outer bound. Idle expiry is computed
    from `last_used_at` and needs no column of its own."""

    last_used_at: datetime
    device: SessionDevice = field(default_factory=SessionDevice)
    revoked_at: datetime | None = None
    revoked_reason: RevocationReason | None = None

    @classmethod
    def start(
        cls,
        *,
        user_id: UUID,
        refresh_token_hash: bytes,
        issued_at: datetime,
        lifetime: timedelta,
        device: SessionDevice | None = None,
        token_family: UUID | None = None,
    ) -> "UserSession":
        """Builds a new, never-persisted session.

        `issued_at` is a **parameter, not a `datetime.now()` call** — AD-07
        forbids the domain from reading the clock, so the application
        layer, which holds the injected clock port, passes the instant in.

        `id` is generated here rather than by the database (DB-07): a
        UUIDv7 minted in Python is known before the insert, which is what
        lets `token_family` default to it in the same expression.

        `token_family` defaults to this session's own id, which is what
        makes a fresh sign-in the root of a new chain. A rotation passes
        the parent's family explicitly, and that is the *only* way a
        session joins an existing one.

        `last_used_at` starts equal to `created_at` rather than null. A
        session that has never been refreshed has still been used — it was
        just created by a sign-in — and a nullable column here would put a
        `None` check in front of every idle-expiry comparison for no
        information gained.
        """
        session_id = generate_uuid7()
        return cls(
            id=session_id,
            user_id=user_id,
            refresh_token_hash=refresh_token_hash,
            token_family=token_family or session_id,
            created_at=issued_at,
            expires_at=issued_at + lifetime,
            last_used_at=issued_at,
            device=device or SessionDevice(),
        )

    # --- state ---------------------------------------------------------------

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired_at(self, instant: datetime) -> bool:
        """Absolute expiry. `instant >= expires_at`, not `>` — `expires_at`
        is when the session stops being valid, not the last instant it
        is."""
        return instant >= self.expires_at

    def is_idle_at(self, instant: datetime, idle_timeout: timedelta) -> bool:
        """Idle expiry, computed rather than stored.

        Takes the timeout as a parameter rather than reading configuration
        — a domain object that read settings would be a domain object that
        cannot be tested at two different policies.
        """
        return instant >= self.last_used_at + idle_timeout

    def is_usable_at(self, instant: datetime, idle_timeout: timedelta) -> bool:
        """The single question the refresh path actually asks.

        Exists so that no caller can check two of the three conditions and
        forget the third — which is the failure mode that leaves revoked
        sessions working, and it fails silently.
        """
        return not (
            self.is_revoked or self.is_expired_at(instant) or self.is_idle_at(instant, idle_timeout)
        )

    # --- transitions ---------------------------------------------------------

    def touch(self, instant: datetime) -> None:
        """Records that the session was just used, sliding the idle window
        forward. Does not extend `expires_at` — that is what makes the
        absolute bound absolute."""
        self.last_used_at = instant

    def revoke(self, *, at: datetime, reason: RevocationReason) -> None:
        """Marks the session unusable, permanently.

        **Idempotent, and the first revocation wins.** A session revoked
        for `reuse_detected` must not be quietly overwritten to `player`
        by a later sign-out — the security-relevant reason is the one that
        happened first, and losing it would erase the only record that an
        attack was detected. Re-revoking is otherwise a no-op so that a
        retried request does not error (CLAUDE.md §3 rule 8).
        """
        if self.is_revoked:
            return
        self.revoked_at = at
        self.revoked_reason = reason
