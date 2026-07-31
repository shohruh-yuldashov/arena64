"""The ports `auth`'s use cases program against — AD-06: declared in
`application/`, satisfied by `infrastructure/`.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.modules.auth.domain.sessions import RevocationReason, UserSession
from app.modules.auth.domain.tokens import TokenClaims, TokenType
from app.modules.auth.domain.verification import EmailVerificationToken


class PasswordHasher(Protocol):
    """Turns a plaintext password into a storable encoded hash.

    A port rather than a direct `argon2` import so the service can be
    tested with a fast stub — real Argon2id is *deliberately* ~50ms per
    call, and a service suite that hashes for real would spend all its
    time proving a library works rather than proving orchestration does.

    A64-011.1 published `hash` alone, on the grounds that an unused method
    on a security interface reads as "this is wired up" to whoever adds
    login next. A64-011.2 is that task, so `verify` and `needs_rehash`
    join it here — together, because a `verify` without a `needs_rehash`
    silently freezes every account at whatever parameters it was first
    hashed with.
    """

    async def hash(self, plaintext: str) -> str:
        """Returns the encoded hash — algorithm, parameters, salt and
        digest in one string. Never returns or logs the plaintext."""
        ...

    async def verify(self, encoded_hash: str, plaintext: str) -> bool:
        """Whether `plaintext` is the password `encoded_hash` was made from.

        Returns a plain `bool` rather than raising on mismatch: a wrong
        password is the single most ordinary outcome this platform has,
        and the caller has to treat "no such account" and "wrong password"
        identically anyway (see `AuthenticationService`). An exception for
        one and not the other is how that symmetry gets broken by accident.

        A *malformed* stored hash is different and does raise — it means
        the database holds something that is not a credential, which no
        sign-in logic can recover from and operators must be told about.

        The comparison is constant-time with respect to the digest; the
        implementation must not compare hashes with `==`.
        """
        ...

    async def needs_rehash(self, encoded_hash: str) -> bool:
        """Whether `encoded_hash` was made with weaker parameters than the
        ones currently configured.

        Reading the parameters back out of the encoding is what makes
        raising Argon2's cost possible at all without a mass password
        reset (database.md §14.2): a sign-in verifies against the
        parameters the hash was made with, and this says whether to
        re-derive it at today's.
        """
        ...

    async def dummy_hash(self) -> str:
        """A hash of nothing in particular, at the current parameters, for
        a caller that must spend a verification's worth of time without
        having a real credential to verify against.

        On the port rather than left to the caller because getting it
        wrong is silent: a hardcoded constant would drift from the
        configured cost the moment those are raised, and the timing
        equalisation it exists to provide would quietly stop working with
        nothing failing. The implementation that owns the parameters is
        the one that can keep it honest.

        Never a real user's hash, and never derived from any real
        password.
        """
        ...


class TokenProvider(Protocol):
    """Signs and verifies tokens. Knows nothing about *why* one is issued.

    The deliberate split from `AccessTokenService` and `TokenValidator`:
    this port is about cryptography and encoding, those are about use
    cases. `AccessTokenService` decides that an access token lasts fifteen
    minutes and speaks for a particular account; this decides only how a
    claim set becomes a signed string and back. That line is what lets
    A64-011.4 add refresh tokens, and a later task add WebSocket tickets
    (AD-09), by writing a new *service* against this same port rather than
    a second signing implementation — and two independent signing
    implementations on one platform is how one of them ends up without
    audience checking.

    A `Protocol`, not an ABC, so a test can substitute a deterministic
    fake without inheriting from the real thing.

    **Synchronous on purpose.** HMAC-SHA256 over a few hundred bytes is
    microseconds — four orders of magnitude below the ~20ms Argon2
    verification that forced `PasswordHasher` onto a worker thread. Making
    these `async` would buy nothing and would cost an `await` on the hot
    path of every authenticated request on the platform.
    """

    def issue(
        self,
        *,
        subject: str,
        token_type: TokenType,
        lifetime_seconds: int,
        custom: Mapping[str, Any] | None = None,
    ) -> tuple[str, TokenClaims]:
        """Mints a signed token and returns it alongside the claims it
        carries.

        Both, rather than just the string, because the caller almost
        always needs the `exp` it just created — a login response has to
        tell the client when to refresh, and re-decoding a token to learn
        what you put in it a microsecond ago is absurd.

        `jti` and `iat` are generated here, not accepted as arguments: a
        caller that could choose a token's own identifier could mint two
        tokens with the same one, which would make a `jti` denylist
        (A64-011.4) silently revoke the wrong token.
        """
        ...

    def decode(self, token: str, *, expected_type: TokenType) -> TokenClaims:
        """Verifies a token completely, or raises.

        Verifies, in the order that matters: the signature (against every
        active key — see `JWTSettings.previous_secret_keys`), then `exp`,
        `iss`, `aud`, and finally `expected_type`. There is no partial
        result and no "skip verification" argument; holding a `TokenClaims`
        means every one of those passed.

        `expected_type` is required rather than defaulted, because the
        default anyone would pick is `ACCESS`, and the one call site that
        forgets to override it is the one that accepts a refresh token
        where an access token belongs.

        Raises `ExpiredToken`, `InvalidSignature`, or `InvalidToken` —
        never a `jwt.*` exception, which would leak the library's
        vocabulary into every caller and make swapping it a rewrite.
        """
        ...


class SessionRepository(Protocol):
    """Collection-like access to the `UserSession` aggregate.

    A `Protocol`, not an ABC: the SQLAlchemy adapter and the test fake
    satisfy it structurally, and one shared contract suite is run against
    both (repositories.md RP-05) so the fake cannot quietly diverge.

    **Every method here is a single storage operation with no opinion in
    it** (repositories.md §2 consequence 3). "Is this session still
    usable" is `SessionService`'s decision, not this port's — which is why
    `get_session` returns revoked and expired sessions rather than
    filtering them out. A repository that hid revoked rows
    would make reuse detection impossible: detecting reuse *requires*
    finding the revoked session the attacker presented.

    Flushes, never commits. The unit of work owns the transaction
    (repositories.md §5.1).
    """

    async def create_session(self, session: UserSession) -> UserSession:
        """Persists a new session and returns it."""
        ...

    async def get_session(self, refresh_token_hash: bytes) -> UserSession | None:
        """The refresh path's lookup: hash the presented token, find its
        session.

        `None` rather than raising — absence is an ordinary outcome for
        the port. Returns the row **whatever its state**: revoked,
        expired, idle. See this class's docstring.
        """
        ...

    async def get_by_id(self, session_id: UUID) -> UserSession | None:
        """Lookup by identity, for revocation of a session a player picked
        from a list. Deliberately separate from `get_session`: one takes a
        secret, the other takes an identifier, and conflating them is how
        a session id ends up being accepted as a credential."""
        ...

    async def update_last_used(self, session_id: UUID, instant: datetime) -> bool:
        """Slides the idle window forward. Returns whether a row matched.

        A targeted `UPDATE` of one column rather than a load-mutate-save,
        because this runs on every refresh and must not read, map and
        rewrite eleven columns to change one — and because a full-row
        write would race with a concurrent revocation and could resurrect
        it.
        """
        ...

    async def revoke_session(
        self, session_id: UUID, *, at: datetime, reason: RevocationReason
    ) -> bool:
        """Revokes one session. Returns whether this call was the one that
        did it.

        `False` for an already-revoked session, so the caller can tell a
        real revocation from a no-op without a second query — and so the
        first reason recorded is never overwritten by a later one. That
        matters: a `reuse_detected` overwritten by a subsequent `player`
        would erase the only record that an attack was found.
        """
        ...

    async def revoke_all_sessions(
        self,
        user_id: UUID,
        *,
        at: datetime,
        reason: RevocationReason,
        except_session_id: UUID | None = None,
    ) -> int:
        """Revokes every live session for a user; returns how many.

        `except_session_id` exists for SE-1 — "a password change revokes
        every session except the one performing it". Without it, changing
        your password signs you out of the device you changed it on, which
        users read as a bug and which makes the security action feel like
        a punishment.

        One statement, not a loop over `revoke_session`: a loop is N round
        trips at the moment of a suspension or a detected compromise,
        which is exactly when latency is least affordable.
        """
        ...

    async def revoke_family(
        self, token_family: UUID, *, at: datetime, reason: RevocationReason
    ) -> int:
        """Revokes an entire rotation chain — database.md §14.3's reuse
        response.

        Separate from `revoke_all_sessions` because it is a different
        blast radius: reuse detection kills the compromised chain, not
        every device the player owns. Signing someone out of their phone
        because their laptop's token was replayed is a worse outcome than
        the attack in most cases.
        """
        ...

    async def list_user_sessions(
        self, user_id: UUID, *, include_revoked: bool = False
    ) -> list[UserSession]:
        """Every session for a user, newest first — SE-2's revocation list.

        Not paginated, deliberately, and this is the one place the
        platform's keyset-by-default rule (RP-03) does not apply: the
        result set is bounded by how many devices a person owns. A
        paginated device list would be more machinery than the data
        justifies. If that assumption ever breaks it breaks visibly, as a
        slow query on a known-small table.

        `include_revoked` defaults to `False` because the common caller is
        "show me my active devices". History is available but is never the
        default.
        """
        ...


class VerificationTokenRepository(Protocol):
    """Collection-like access to the `EmailVerificationToken` aggregate.

    A `Protocol`, not an ABC: the SQLAlchemy adapter and the test fake
    satisfy it structurally and one shared contract suite runs against
    both (repositories.md RP-05).

    **No opinions** (repositories.md §2 consequence 3). `get_by_hash`
    returns used and expired tokens rather than filtering them out —
    "is this token still redeemable" is `EmailVerificationService`'s
    decision, and a repository that hid used rows would make replay
    detection impossible for the same reason hiding revoked sessions would
    have made refresh-token reuse detection impossible.

    Flushes, never commits. The unit of work owns the transaction
    (repositories.md §5.1).
    """

    async def create(self, token: EmailVerificationToken) -> EmailVerificationToken:
        """Persists a newly issued token and returns it."""
        ...

    async def get_by_hash(self, token_hash: bytes) -> EmailVerificationToken | None:
        """The redemption path's lookup: hash the presented token, find its
        row. `None` for an unknown digest — an ordinary outcome for the
        port. Returns the row **whatever its state**."""
        ...

    async def invalidate_active_for_user(self, user_id: UUID, *, at: datetime) -> int:
        """Marks every unused token for a user as used; returns how many.

        The mechanism behind two requirements at once: "invalidate
        previous tokens" on resend, and "invalidate remaining active
        tokens" after a successful verification. One statement rather than
        a loop, so it is atomic with respect to a concurrent resend.

        `used_at` rather than a deletion, because §4.5's scheduled
        hard-delete is a separate concern with its own retention window —
        and because a row that vanishes cannot be distinguished from one
        that never existed when investigating a replay.
        """
        ...

    async def count_active_for_user(self, user_id: UUID, *, at: datetime) -> int:
        """How many unused, unexpired tokens a user has.

        Exists for the tests that assert the at-most-one-live invariant
        the partial unique index enforces, and for a future resend
        throttle. Takes `at` rather than reading the clock (AD-07) —
        "active" is a question about an instant.
        """
        ...
