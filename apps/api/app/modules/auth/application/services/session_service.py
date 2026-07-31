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

A single sign-in's family contains one session today. It becomes a chain
when A64-011.5 wires rotation — see `rotate_refresh_token`, which is
prepared but not implemented, per this task's brief.

## Why revocation and validation are separate transactions

Reuse detection *writes* during what a caller thinks is a read. That is
correct and it is why `validate_refresh_token` opens a unit of work: the
revocation must be durable before the caller is told the token was
rejected, or a crash between the two leaves the compromised family live
with nothing recording that it was detected.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from app.config.settings import SessionSettings
from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.auth.application.ports import SessionRepository
from app.modules.auth.application.services.refresh_token_service import RefreshTokenService
from app.modules.auth.domain.exceptions import (
    ExpiredRefreshToken,
    RevokedSession,
    SessionNotFound,
)
from app.modules.auth.domain.sessions import RevocationReason, SessionDevice, UserSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedRefreshToken:
    """A new session and the one and only copy of its raw token.

    A plain frozen dataclass, not a Pydantic model, for the reason
    `users.public.credentials.UserCredentials` is: a Pydantic model is one
    keystroke from being a FastAPI `response_model`, and a type whose
    whole purpose is to carry a live credential must not be that. When
    A64-011.5 adds the refresh endpoint it will declare its own wire
    schema and copy the token across deliberately.

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
        unit_of_work: UnitOfWork,
        clock: Clock,
        settings: SessionSettings,
    ) -> None:
        self._sessions = sessions
        self._tokens = tokens
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

    async def validate_refresh_token(self, refresh_token: str) -> UserSession:
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
        session = await self._sessions.get_session(presented_hash)

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
        """Exchanges a valid refresh token for a fresh one — **not
        implemented in A64-011.4**.

        The task's brief is "prepare interface only", and this is that
        interface: the signature A64-011.5's refresh endpoint will call,
        fixed now so the endpoint is written against a stable shape.

        It raises rather than returning something plausible. A method that
        quietly issued a token *without* invalidating the old one would
        look like it worked while disabling reuse detection entirely —
        every old token would stay valid, and the property §14.3 exists to
        provide would be silently absent. Failing loudly is the only safe
        placeholder for a security operation.

        What A64-011.5 must implement here, in one transaction:

        1. `validate_refresh_token` — including reuse detection.
        2. Revoke the presented session with a `rotated` reason (a new
           `RevocationReason` member; the enum needs a migration to add
           it, which is why it is not pre-declared here).
        3. Create a successor carrying **the same `token_family`** and the
           parent's `expires_at` — a rotation must not extend the absolute
           window, or a chain refreshed daily never expires and the 30-day
           bound means nothing.
        4. Return the successor and its raw token.

        Step 3's two details are the ones that are easy to get wrong and
        silent when wrong: a new family per rotation disables reuse
        detection, and a refreshed absolute expiry disables the absolute
        bound.
        """
        raise NotImplementedError(
            "Refresh token rotation is A64-011.5. See this method's docstring for "
            "the contract it must satisfy; issuing a token without invalidating "
            "its predecessor would disable reuse detection."
        )

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
