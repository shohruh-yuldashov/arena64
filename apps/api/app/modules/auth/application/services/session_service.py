"""`SessionService` — create, validate, revoke. The refresh session's use
cases.

Orchestrates; does not compute (services.md §3.2). Token generation and
hashing live in `RefreshTokenService`, the rules about what a session *is*
live in the entity, storage lives behind `SessionRepository`. What lives
here is the sequencing, and one thing that is genuinely nowhere else:
**reuse detection**.

Four collaborators, all injected, none constructed inside:

    SessionRepository    storage, declared in `application/ports.py` (AD-06)
    RefreshTokenService  generate / hash / verify
    UnitOfWork           the transaction boundary (services.md §9.1)
    Clock                "now", because AD-07 forbids reading it directly

## The raw token is returned exactly once, and never stored

`create_session` returns `IssuedRefreshToken`, which carries the
plaintext. That object is the only place it ever exists on this side of
the wire, and the session row that was just written holds only its
SHA-256 digest (database.md §14.3: "the token itself exists only in
transit and in the client"). There is no method here that can hand a
caller the plaintext of an existing session, because there is nowhere to
retrieve it from — which is the property that makes a database read, a
backup or a support query unable to yield a working credential.

## Reuse detection, and why it revokes the whole family

`validate_refresh_token` finds a session by hashing the presented token.
If that session exists but is **already revoked**, this is the case
database.md §14.3 names: the token was captured.

The response is to revoke the entire `token_family`, not just the
presented link, and the doc's reasoning is exact — "the attacker and the
legitimate user now both hold links in the same chain, and there is no
way to tell which one is presenting". Revoking only the presented link
leaves the other party's token working, and there is no way to know which
party that is.

This is deliberately aggressive: it signs a real user out of that device.
The alternative is leaving an attacker holding a working credential for
up to thirty days, which is not a trade this platform should make.

A single sign-in's family is one session until the first refresh; from
then on it is a chain, because `rotate_refresh_token` (A64-011.5) revokes
the presented link with reason `ROTATED` and issues its successor into the
same family.

## Why revocation and validation are separate transactions

Reuse detection *writes* during what a caller thinks is a read. That is
correct and it is why `validate_refresh_token` opens a unit of work: the
revocation must be durable before the caller is told the token was
rejected, or a crash between the two leaves the compromised family live
with nothing recording that it was detected.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from app.config.settings import SessionSettings
from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.admin.public import AccountRestrictionGate
from app.modules.auth.application.ports import SessionRepository
from app.modules.auth.application.services.refresh_token_service import RefreshTokenService
from app.modules.auth.domain.exceptions import (
    AccountRestricted,
    ConcurrentRotation,
    ExpiredRefreshToken,
    RevokedSession,
    SessionNotFound,
)
from app.modules.auth.domain.sessions import RevocationReason, SessionDevice, UserSession

logger = logging.getLogger(__name__)

#: What a client racing itself is told to wait — A64-028.2 §3.
#:
#: The successor is already in the browser's cookie jar by the time this
#: answer is written; what the caller is waiting for is its own other tab's
#: response to have been *applied*, which has either happened or is about to.
#: A second is long enough for that and short enough that a person does not
#: see it.
_ROTATION_RETRY_AFTER_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class IssuedRefreshToken:
    """A new session and the one and only copy of its raw token.

    A plain frozen dataclass, not a Pydantic model, for the reason
    `users.public.credentials.UserCredentials` is: a Pydantic model is one
    keystroke from being a FastAPI `response_model`, and a type whose
    whole purpose is to carry a live credential must not be that.
    `presentation/schemas/tokens.py::TokenPair` is the wire schema that
    copies the token across deliberately — see its docstring on why the
    duplication is the point rather than an oversight.

    `repr=False` on the token is not decoration — a dataclass repr lands
    in tracebacks and in every error reporter that walks frame locals, and
    a refresh token in a bug report is a thirty-day credential
    (services.md §8.5).
    """

    session: UserSession
    refresh_token: str = field(repr=False)


class SessionService:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        tokens: RefreshTokenService,
        restrictions: AccountRestrictionGate,
        unit_of_work: UnitOfWork,
        clock: Clock,
        settings: SessionSettings,
    ) -> None:
        self._sessions = sessions
        self._tokens = tokens
        self._restrictions = restrictions
        self._uow = unit_of_work
        self._clock = clock
        self._settings = settings

    # --- creation -----------------------------------------------------------

    async def create_session(
        self,
        user_id: UUID,
        *,
        device: SessionDevice | None = None,
    ) -> IssuedRefreshToken:
        """Starts a new session for a user and returns its refresh token.

        Called after `AuthenticationService.authenticate` has proven
        identity. Nothing here re-checks whether the account is active or
        locked: that is the authentication service's job and it has
        already run, and a second copy of the sign-in rules in the service
        that issues sessions is a second copy that will drift.

        Each call starts a **new family**. That is what makes multiple
        devices work: signing in on a phone does not join the laptop's
        rotation chain, so reuse detection on one cannot sign the other
        out.
        """
        now = self._clock.now()
        raw_token = self._tokens.generate_refresh_token()

        session = UserSession.start(
            user_id=user_id,
            refresh_token_hash=self._tokens.hash_refresh_token(raw_token),
            issued_at=now,
            lifetime=timedelta(days=self._settings.refresh_token_ttl_days),
            device=device,
        )

        async with self._uow:
            created = await self._sessions.create_session(session)
            await self._uow.commit()

        # Identifiers only — never the token, never the user agent, never
        # the IP. The first is a credential; the other two are Personal
        # data under database.md §14.1, and a log line is a permanent
        # record with broader read access than the database
        # (services.md §8.5).
        logger.info(
            "session_created",
            extra={
                "user_id": str(user_id),
                "session_id": str(created.id),
                "token_family": str(created.token_family),
            },
        )
        return IssuedRefreshToken(session=created, refresh_token=raw_token)

    # --- validation ---------------------------------------------------------

    async def validate_refresh_token(
        self, refresh_token: str, *, for_update: bool = False
    ) -> UserSession:
        """Exchanges a presented token for the session it identifies.

        Raises `SessionNotFound` when nothing matches, `RevokedSession`
        when the session was revoked — **after** revoking its whole family,
        see below — and `ExpiredRefreshToken` when the absolute or idle
        window has elapsed. All four are `InvalidRefreshToken`, all four
        are 401, and all four carry the same message.

        Does not rotate and does not mark the session used. Both belong to
        the refresh *use case*, which is A64-011.5's — this answers only
        "is this token currently exchangeable, and for which session".
        """
        presented_hash = self._tokens.hash_refresh_token(refresh_token)
        session = await self._sessions.get_session(presented_hash, for_update=for_update)

        if session is None:
            # No row matched the digest. Either a forged token, or one
            # from a session that has been swept. Indistinguishable, and
            # deliberately reported identically.
            logger.info("refresh_rejected", extra={"reason": "no_matching_session"})
            raise SessionNotFound("The refresh token is not valid.")

        # Belt and braces. The lookup above already matched on the digest,
        # so this can only fail if storage returned the wrong row — but a
        # repository that returned "close enough" would otherwise hand a
        # caller someone else's session, and that is worth one constant-time
        # comparison to make impossible.
        if not self._tokens.verify_refresh_token(refresh_token, session.refresh_token_hash):
            logger.error(
                "refresh_token_hash_mismatch",
                extra={"session_id": str(session.id)},
            )
            raise SessionNotFound("The refresh token is not valid.")

        if session.is_revoked:
            # A64-028.2 §3. Two questions, not one: *was* this token rotated
            # away, and is this the client's own tab arriving a moment late?
            if await self._is_concurrent_rotation(session, at=self._clock.now()):
                logger.info(
                    "refresh_rotation_conflict",
                    extra={"token_family": str(session.token_family)},
                )
                raise ConcurrentRotation(
                    "This session was refreshed by another request. Try again.",
                    retry_after_seconds=_ROTATION_RETRY_AFTER_SECONDS,
                )
            await self._handle_reuse(session)
            raise RevokedSession("The refresh token is not valid.")

        now = self._clock.now()
        idle_timeout = timedelta(days=self._settings.idle_timeout_days)
        if session.is_expired_at(now) or session.is_idle_at(now, idle_timeout):
            # One exception for both windows: the client signs in again
            # either way, and saying *which* window elapsed would disclose
            # when the legitimate user last used the session.
            logger.info(
                "refresh_rejected",
                extra={"reason": "expired", "session_id": str(session.id)},
            )
            raise ExpiredRefreshToken("The session has expired. Sign in again.")

        return session

    async def _is_concurrent_rotation(self, session: UserSession, *, at: datetime) -> bool:
        """Whether a revoked token is this client racing itself — §3, §4.

        A64-028.1 proved the failure this answers: a browser shares one
        cookie jar across its tabs, so two tabs refreshing together present
        the *same* token, the second arrives after the first has rotated it,
        and reuse detection burned the family — including the successor the
        first tab had just been issued. Both tabs were signed out and the
        platform's only theft signal fired on entirely ordinary traffic.

        Three conditions, and each excludes a case §4 requires to stay
        rejected:

          `ROTATED`      only a rotation is a race. A token revoked by
                         sign-out, password change, suspension or a previous
                         reuse detection was revoked *on purpose*, and
                         presenting it again is exactly what those
                         revocations exist to refuse (cases C and E)
          within grace   the two requests are milliseconds apart; what
                         separates them is one round trip. Outside the
                         window a rotated token being replayed is case B —
                         a credential someone kept — and takes the reuse
                         path unchanged
          live family    a race means there is a successor in use. If the
                         chain has already been signed out or burned, a
                         replay of one of its links is not a tab losing a
                         race

        **This grants nothing.** The caller is refused either way; the only
        difference is whether the refusal also destroys a live session and
        raises a security alert. Reuse detection is not relaxed — it is
        stopped from firing on a case that was never reuse.
        """
        if session.revoked_reason is not RevocationReason.ROTATED:
            return False

        grace = timedelta(seconds=self._settings.rotation_grace_seconds)
        if grace <= timedelta(0):
            return False

        revoked_at = session.revoked_at
        # Belt and braces: `is_revoked` is `revoked_at is not None`, so this
        # cannot be None here — but a row written by a future path that set
        # only the reason must not be read as "revoked at the epoch".
        if revoked_at is None or at - revoked_at > grace:
            return False

        return await self._sessions.family_has_live_session(session.token_family)

    async def _handle_reuse(self, session: UserSession) -> None:
        """database.md §14.3's reuse response, in full.

        A revoked session's token was presented. If the revocation was an
        ordinary sign-out this is a client that has not noticed; if it was
        a rotation, the token was captured. The platform cannot tell the
        two apart from the request, so it treats the family as
        compromised — which costs a legitimate user a sign-in and denies
        an attacker a thirty-day credential.

        Logged at WARNING, not INFO. Every other refresh rejection is a
        normal outcome under BE-07; this one is the only signal the
        platform has that a token was replayed, and the *rate* of it is
        what an alert should watch.
        """
        revoked = await self._revoke_family(session, reason=RevocationReason.REUSE_DETECTED)

        logger.warning(
            "refresh_token_reuse_detected",
            extra={
                "user_id": str(session.user_id),
                "session_id": str(session.id),
                "token_family": str(session.token_family),
                "sessions_revoked": revoked,
                "original_reason": session.revoked_reason,
            },
        )

    async def _revoke_family(self, session: UserSession, *, reason: RevocationReason) -> int:
        async with self._uow:
            revoked = await self._sessions.revoke_family(
                session.token_family, at=self._clock.now(), reason=reason
            )
            await self._uow.commit()
        return revoked

    # --- rotation -----------------------------------------------------------

    async def rotate_refresh_token(self, refresh_token: str) -> IssuedRefreshToken:
        """Exchanges a valid refresh token for a fresh one, invalidating it.

        database.md §14.3's "rotation on every use, with the old token
        invalidated". A64-011.4 fixed this signature and left the body
        raising; A64-011.5 implements it because `POST /auth/refresh` is
        its caller.

        Raises everything `validate_refresh_token` raises, and for the same
        reasons — including reuse detection, which fires here first and is
        what makes rotation a security mechanism rather than churn.

        ## The two details that are silent when wrong

        **The successor inherits `token_family`.** A rotation that started
        a new family would sever the chain, and reuse detection revokes by
        family — so a captured token would be detected but only the single
        presented link would be revoked, leaving the attacker's other links
        live. Rotation without an inherited family is rotation with the
        security property removed.

        **The successor inherits `expires_at`.** A rotation must not extend
        the absolute window: a chain refreshed daily would otherwise never
        expire, and the 30-day bound — the only thing limiting a captured
        token whose theft is never detected — would mean nothing. The idle
        window *does* slide, because `last_used_at` on the successor is
        now; that is the point of the two-expiry design.

        ## Why the old session is revoked before the new one is created

        Both writes are in one transaction, so ordering does not change
        the committed outcome. It changes the *uncommitted* one: if
        creation fails — a hash collision, a constraint, a lost connection
        — the rollback leaves the original session live rather than
        leaving the caller with no usable token. Failing closed here would
        sign a legitimate user out for an infrastructure hiccup.

        ## Why an administrative restriction is checked here

        A64-024.6. Suspension revokes every live session (SE-3), so a
        restricted account normally has no token to present. This closes
        the narrow race that remains: a rotation already in flight when the
        revocation commits inserts a successor the revoking `UPDATE` never
        saw, because the row did not exist in its snapshot.

        Checking here makes the successor useless the moment it is asked to
        rotate again — and, more importantly, makes this path agree with
        sign-in, which is the same question asked at the same kind of
        boundary. `domain-model.md` DM-12 names both: the sanction is read
        "on every sign-in".
        """
        # `for_update` — A64-028.2 §29, §30. The read and the two writes
        # below share one transaction (`SessionUnitOfWork` opens none of its
        # own), so locking the row here serialises concurrent rotations of
        # the same token: one succeeds, the rest find it revoked and take
        # §3's benign-race path. Without it both callers can read the row
        # live and mint a successor each, which is not a security failure
        # but does put two devices in a session list that holds one.
        #
        # One row, by unique index, for the length of a two-statement
        # transaction. No table lock, and nothing to deadlock against: a
        # rotation only ever locks the single row it presented.
        session = await self.validate_refresh_token(refresh_token, for_update=True)

        now = self._clock.now()

        if await self._restrictions.restriction_for(session.user_id, at=now):
            # Revoked rather than merely refused: a browser holding a
            # credential this server will never accept again should stop
            # holding it, and leaving the chain live would make every
            # subsequent attempt a fresh reuse-detection puzzle.
            await self.revoke_session(session.id, reason=RevocationReason.SUSPENSION)
            logger.info(
                "refresh_rejected",
                extra={"user_id": str(session.user_id), "reason": "restricted_account"},
            )
            raise AccountRestricted("This account is currently unavailable.")
        raw_token = self._tokens.generate_refresh_token()
        successor = UserSession.start(
            user_id=session.user_id,
            refresh_token_hash=self._tokens.hash_refresh_token(raw_token),
            issued_at=now,
            # Deliberately *not* the configured TTL: the successor inherits
            # what remains of the original absolute window.
            lifetime=session.expires_at - now,
            device=session.device,
            token_family=session.token_family,
        )

        async with self._uow:
            await self._sessions.revoke_session(session.id, at=now, reason=RevocationReason.ROTATED)
            created = await self._sessions.create_session(successor)
            await self._uow.commit()

        logger.info(
            "session_rotated",
            extra={
                "user_id": str(session.user_id),
                "session_id": str(created.id),
                "previous_session_id": str(session.id),
                "token_family": str(created.token_family),
            },
        )
        return IssuedRefreshToken(session=created, refresh_token=raw_token)

    # --- revocation ---------------------------------------------------------

    async def revoke_session(
        self,
        session_id: UUID,
        *,
        reason: RevocationReason = RevocationReason.PLAYER,
    ) -> bool:
        """Revokes one session. Returns whether this call revoked it.

        `False` means it was already revoked, which is a successful no-op
        rather than an error — a caller retrying after a dropped response
        must not get a failure for the retry (CLAUDE.md §3 rule 8).

        Raises `SessionNotFound` only when no such session exists at all.
        """
        session = await self._sessions.get_by_id(session_id)
        if session is None:
            raise SessionNotFound("The session does not exist.")

        async with self._uow:
            revoked = await self._sessions.revoke_session(
                session_id, at=self._clock.now(), reason=reason
            )
            await self._uow.commit()

        if revoked:
            logger.info(
                "session_revoked",
                extra={
                    "user_id": str(session.user_id),
                    "session_id": str(session_id),
                    "reason": reason.value,
                },
            )
        return revoked

    async def revoke_by_refresh_token(
        self,
        refresh_token: str,
        *,
        reason: RevocationReason = RevocationReason.PLAYER,
    ) -> bool:
        """Signs one device out, given its refresh token.

        The sign-out counterpart to `rotate_refresh_token`, added by
        A64-011.5 for `POST /auth/logout`. It takes a token rather than a
        session id because an access token names a *user*, not a session —
        there is no `sid` claim — so the refresh token is the only
        credential that says *which device*.

        **Deliberately not built on `validate_refresh_token`**, and this
        is the substantive decision. That method treats a revoked session
        as reuse and burns the whole family; doing so here would mean a
        client that sends logout twice — a retry, a double-clicked button,
        a page unload racing a fetch — signs the user out of the successor
        session too, and logs a security alert for it. Signing out is not
        an attack, and the endpoint must be idempotent.

        Expiry is likewise not an error. A caller signing out of a session
        that has already lapsed wanted exactly the state it is now in.

        Returns whether this call was the one that revoked it. `False`
        means it was already revoked, which the endpoint reports as
        success.

        Raises `SessionNotFound` when no session matches at all — a
        garbage or forged token. That is the one case where "you are
        signed out" would be a lie.
        """
        session = await self._sessions.get_session(self._tokens.hash_refresh_token(refresh_token))
        if session is None:
            logger.info("logout_rejected", extra={"reason": "no_matching_session"})
            raise SessionNotFound("The refresh token is not valid.")

        if session.is_revoked:
            # Already gone. The caller's intent is satisfied.
            return False

        return await self.revoke_session(session.id, reason=reason)

    async def revoke_all_sessions(
        self,
        user_id: UUID,
        *,
        reason: RevocationReason = RevocationReason.PLAYER,
        except_session_id: UUID | None = None,
    ) -> int:
        """Revokes every live session for a user; returns how many.

        The mechanism behind three requirements that are otherwise
        unimplementable: SE-1 (a password change revokes every session but
        the one performing it — pass `except_session_id`), SE-3 (a
        suspension revokes all sessions immediately), and the
        "log out everywhere" a player can ask for.

        Idempotent: calling it twice revokes nothing the second time and
        returns `0`.
        """
        async with self._uow:
            revoked = await self._sessions.revoke_all_sessions(
                user_id,
                at=self._clock.now(),
                reason=reason,
                except_session_id=except_session_id,
            )
            await self._uow.commit()

        logger.info(
            "all_sessions_revoked",
            extra={
                "user_id": str(user_id),
                "sessions_revoked": revoked,
                "reason": reason.value,
                "kept_session_id": str(except_session_id) if except_session_id else None,
            },
        )
        return revoked

    # --- listing ------------------------------------------------------------

    async def list_user_sessions(
        self, user_id: UUID, *, include_revoked: bool = False
    ) -> list[UserSession]:
        """SE-2's device list. Read-only: opens no transaction.

        Returns entities carrying `refresh_token_hash`. That is safe
        inside the module and must not stay that way at the boundary —
        A64-011.5's endpoint needs a DTO without it, exactly as
        `users.public.UserRead` has no `password_hash`.
        """
        return await self._sessions.list_user_sessions(user_id, include_revoked=include_revoked)
