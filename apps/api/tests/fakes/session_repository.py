"""An in-memory `SessionRepository` — repositories.md RP-05's fake.

Satisfies the same `Protocol` as `SqlAlchemySessionRepository`
structurally, and is held to the same contract suite
(`tests/contract/test_session_repository.py`), which is the only thing
that keeps the two honest about each other.

The behaviours it goes out of its way to reproduce are the ones a naive
dict-backed fake gets wrong, and a service would then be tested against
incorrectly:

  - **"first revocation wins"** — every revoke method skips already-revoked
    rows, exactly as the real adapter's `WHERE revoked_at IS NULL` does,
    and returns the same counts. A fake that revoked unconditionally would
    let a service test pass while production silently kept the earlier
    reason;
  - **`update_last_used` refuses revoked sessions**, matching the real
    `WHERE` clause — otherwise a revoked session's "last seen" keeps
    moving in the fake and not in production;
  - **copies on the way in and out**, because the real repository maps
    through an ORM row and therefore hands back a *different object* than
    the caller passed. A fake that stored the caller's instance would let
    a test mutate a "persisted" session without saving and still see the
    change on the next read — passing for a reason production would not;
  - **newest-first ordering with the same `(created_at, id)` key**;
  - **`list_user_devices` collapses a rotation chain**, taking the
    family's earliest `created_at` as its sign-in time, exactly as the
    real adapter's `MIN(created_at) GROUP BY token_family` does. A fake
    that read the live row's own `created_at` would report every device
    as having signed in at its last refresh, and the service test would
    agree with it.
"""

import copy
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.core.exceptions import ConflictError
from app.modules.auth.application.read_models import SessionDeviceSummary
from app.modules.auth.domain.sessions import RevocationReason, UserSession


class FakeSessionRepository:
    def __init__(self, sessions: list[UserSession] | None = None) -> None:
        self._sessions: dict[UUID, UserSession] = {}
        for session in sessions or []:
            self._sessions[session.id] = copy.deepcopy(session)

    # --- writes -------------------------------------------------------------

    async def create_session(self, session: UserSession) -> UserSession:
        # Mirrors the real adapter's unique index on `refresh_token_hash`.
        if any(
            existing.refresh_token_hash == session.refresh_token_hash
            for existing in self._sessions.values()
        ):
            raise ConflictError("Could not create the session.")

        self._sessions[session.id] = copy.deepcopy(session)
        return copy.deepcopy(session)

    async def update_last_used(self, session_id: UUID, instant: datetime) -> bool:
        session = self._sessions.get(session_id)
        if session is None or session.is_revoked:
            return False

        session.last_used_at = instant
        return True

    async def revoke_session(
        self, session_id: UUID, *, at: datetime, reason: RevocationReason
    ) -> bool:
        session = self._sessions.get(session_id)
        if session is None or session.is_revoked:
            return False

        session.revoke(at=at, reason=reason)
        return True

    async def revoke_all_sessions(
        self,
        user_id: UUID,
        *,
        at: datetime,
        reason: RevocationReason,
        except_session_id: UUID | None = None,
    ) -> int:
        return self._revoke_matching(
            lambda session: session.user_id == user_id and session.id != except_session_id,
            at=at,
            reason=reason,
        )

    async def revoke_family(
        self, token_family: UUID, *, at: datetime, reason: RevocationReason
    ) -> int:
        return self._revoke_matching(
            lambda session: session.token_family == token_family, at=at, reason=reason
        )

    def _revoke_matching(
        self,
        predicate: Callable[[UserSession], bool],
        *,
        at: datetime,
        reason: RevocationReason,
    ) -> int:
        matches = 0
        for session in self._sessions.values():
            if session.is_revoked or not predicate(session):
                continue
            session.revoke(at=at, reason=reason)
            matches += 1
        return matches

    # --- reads --------------------------------------------------------------

    async def get_session(
        self, refresh_token_hash: bytes, *, for_update: bool = False
    ) -> UserSession | None:
        # `for_update` is accepted and ignored: a dictionary has no rows to
        # lock, and the property it buys — one successor per token under
        # concurrency — is a real-database question that
        # `tests/contract/test_refresh_concurrency.py` asks of real
        # PostgreSQL. Pretending to honour it here would be a fake that
        # passes while the system is broken.
        for session in self._sessions.values():
            if session.refresh_token_hash == refresh_token_hash:
                return copy.deepcopy(session)
        return None

    async def family_has_live_session(self, token_family: UUID) -> bool:
        return any(
            session.token_family == token_family and not session.is_revoked
            for session in self._sessions.values()
        )

    async def get_by_id(self, session_id: UUID) -> UserSession | None:
        found = self._sessions.get(session_id)
        return copy.deepcopy(found) if found is not None else None

    async def list_user_sessions(
        self, user_id: UUID, *, include_revoked: bool = False
    ) -> list[UserSession]:
        matches = [
            session
            for session in self._sessions.values()
            if session.user_id == user_id and (include_revoked or not session.is_revoked)
        ]
        # Same ordering key as the real adapter: newest first, with `id` as
        # the tiebreak so two sessions created in the same millisecond
        # still have a stable order.
        matches.sort(key=lambda session: (session.created_at, session.id), reverse=True)
        return [copy.deepcopy(session) for session in matches]

    async def list_user_devices(self, user_id: UUID) -> list[SessionDeviceSummary]:
        signed_in_at: dict[UUID, datetime] = {}
        for session in self._sessions.values():
            if session.user_id != user_id:
                continue
            # Revoked ancestors count: the family's first row is almost
            # always one, because rotation revokes what it replaces.
            earliest = signed_in_at.get(session.token_family)
            if earliest is None or session.created_at < earliest:
                signed_in_at[session.token_family] = session.created_at

        devices = [
            SessionDeviceSummary(
                token_family=session.token_family,
                signed_in_at=signed_in_at[session.token_family],
                last_used_at=session.last_used_at,
                user_agent=session.device.user_agent,
            )
            for session in self._sessions.values()
            if session.user_id == user_id and not session.is_revoked
        ]
        devices.sort(key=lambda device: (device.signed_in_at, device.token_family), reverse=True)
        return devices

    async def family_belongs_to(self, user_id: UUID, token_family: UUID) -> bool:
        return any(
            session.user_id == user_id and session.token_family == token_family
            for session in self._sessions.values()
        )
