"""The SQLAlchemy `SessionRepository` — `auth`'s only storage adapter.

Maps between `UserSessionModel` rows and `UserSession` domain entities and
does nothing else. repositories.md §4: a repository returns domain
entities, never ORM rows, because a returned row carries lazy loading and
a session lifetime into layers that have no business knowing about
either.

## Why the revocation methods are Core `UPDATE`s

`revoke_session`, `revoke_all_sessions` and `revoke_family` each issue a
single statement with the revocation condition in the `WHERE` clause,
rather than loading rows, calling `UserSession.revoke` and flushing.

Three reasons, in increasing order of importance:

1. `revoke_all_sessions` on a suspension would otherwise be N round trips
   at the exact moment latency matters least affordable.
2. `WHERE revoked_at IS NULL` makes each of them idempotent *in the
   database*, so the returned row count is the honest number of sessions
   this call actually revoked rather than the number it looked at.
3. It closes a read-modify-write race. Two concurrent revocations —
   a player signing out while reuse detection fires — would otherwise
   both load an unrevoked row and the second write would overwrite the
   first's reason. With the condition in the statement, exactly one wins
   and it is the first to commit.

The cost is that these paths do not go through the entity's `revoke`
method, so its "first revocation wins" rule is expressed twice: once in
Python for callers holding an entity, once as `WHERE revoked_at IS NULL`
here. `tests/contract/test_session_repository.py` asserts both agree.
"""

import logging
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.modules.auth.domain.sessions import (
    RevocationReason,
    SessionDevice,
    UserSession,
)
from app.modules.auth.infrastructure.models import UserSessionModel

logger = logging.getLogger(__name__)


class SqlAlchemySessionRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1) — never holds a session longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- mapping ------------------------------------------------------------

    @staticmethod
    def _to_domain(row: UserSessionModel) -> UserSession:
        return UserSession(
            id=row.id,
            user_id=row.user_id,
            refresh_token_hash=row.refresh_token_hash,
            token_family=row.token_family,
            created_at=row.created_at,
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
            device=SessionDevice(
                device_name=row.device_name,
                user_agent=row.user_agent,
                ip_address=row.ip_address,
            ),
            revoked_at=row.revoked_at,
            revoked_reason=row.revoked_reason,
        )

    @staticmethod
    def _to_model(session: UserSession) -> UserSessionModel:
        return UserSessionModel(
            id=session.id,
            user_id=session.user_id,
            refresh_token_hash=session.refresh_token_hash,
            token_family=session.token_family,
            created_at=session.created_at,
            expires_at=session.expires_at,
            last_used_at=session.last_used_at,
            device_name=session.device.device_name,
            user_agent=session.device.user_agent,
            ip_address=session.device.ip_address,
            revoked_at=session.revoked_at,
            revoked_reason=session.revoked_reason,
        )

    # --- writes -------------------------------------------------------------

    async def create_session(self, session: UserSession) -> UserSession:
        row = self._to_model(session)
        self._session.add(row)
        try:
            # Flush, never commit — the unit of work owns the transaction.
            # Flushing here is what makes the unique constraint on
            # `refresh_token_hash` fire now, at the point that has the
            # context to translate it.
            await self._session.flush()
        except IntegrityError as error:
            # Reaching here means two tokens hashed to the same value.
            # With 256 bits from a CSPRNG that is not a collision anyone
            # will ever observe — it means a caller reused a token, which
            # is a defect worth surfacing loudly rather than retrying.
            logger.error("session_token_hash_collision", extra={"user_id": str(session.user_id)})
            raise ConflictError("Could not create the session.") from error

        return self._to_domain(row)

    async def update_last_used(self, session_id: UUID, instant: datetime) -> bool:
        result = await self._execute(
            update(UserSessionModel)
            .where(
                UserSessionModel.id == session_id,
                # Never slides the window on a revoked session. Without
                # this a revoked session's `last_used_at` keeps moving,
                # which makes the revocation list lie about when the
                # device was last seen.
                UserSessionModel.revoked_at.is_(None),
            )
            .values(last_used_at=instant)
        )
        return result.rowcount == 1

    async def revoke_session(
        self, session_id: UUID, *, at: datetime, reason: RevocationReason
    ) -> bool:
        result = await self._execute(
            update(UserSessionModel)
            .where(
                UserSessionModel.id == session_id,
                # "First revocation wins" — see this module's docstring.
                UserSessionModel.revoked_at.is_(None),
            )
            .values(revoked_at=at, revoked_reason=reason)
        )
        return result.rowcount == 1

    async def revoke_all_sessions(
        self,
        user_id: UUID,
        *,
        at: datetime,
        reason: RevocationReason,
        except_session_id: UUID | None = None,
    ) -> int:
        statement = (
            update(UserSessionModel)
            .where(
                UserSessionModel.user_id == user_id,
                UserSessionModel.revoked_at.is_(None),
            )
            .values(revoked_at=at, revoked_reason=reason)
        )
        if except_session_id is not None:
            # SE-1: a password change revokes every session except the one
            # performing it.
            statement = statement.where(UserSessionModel.id != except_session_id)

        return (await self._execute(statement)).rowcount

    async def revoke_family(
        self, token_family: UUID, *, at: datetime, reason: RevocationReason
    ) -> int:
        result = await self._execute(
            update(UserSessionModel)
            .where(
                UserSessionModel.token_family == token_family,
                UserSessionModel.revoked_at.is_(None),
            )
            .values(revoked_at=at, revoked_reason=reason)
        )
        return result.rowcount

    # --- reads --------------------------------------------------------------

    async def family_has_live_session(self, token_family: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(
                    exists().where(
                        UserSessionModel.token_family == token_family,
                        UserSessionModel.revoked_at.is_(None),
                    )
                )
            )
        )

    async def get_session(
        self, refresh_token_hash: bytes, *, for_update: bool = False
    ) -> UserSession | None:
        statement = select(UserSessionModel).where(
            UserSessionModel.refresh_token_hash == refresh_token_hash
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        return self._to_domain(row) if row is not None else None

    async def get_by_id(self, session_id: UUID) -> UserSession | None:
        row = await self._session.get(UserSessionModel, session_id)
        return self._to_domain(row) if row is not None else None

    async def list_user_sessions(
        self, user_id: UUID, *, include_revoked: bool = False
    ) -> list[UserSession]:
        statement = select(UserSessionModel).where(UserSessionModel.user_id == user_id)
        if not include_revoked:
            statement = statement.where(UserSessionModel.revoked_at.is_(None))

        # Newest first — a device list is read top-down and the session
        # someone is looking for is almost always the one they just
        # created. `id` is the tiebreak so the order is total: UUIDv7 is
        # time-ordered, so it agrees with `created_at` rather than
        # fighting it, and two sessions created in the same millisecond
        # still have a stable order.
        statement = statement.order_by(
            UserSessionModel.created_at.desc(), UserSessionModel.id.desc()
        )

        rows = (await self._session.scalars(statement)).all()
        return [self._to_domain(row) for row in rows]

    # --- plumbing -----------------------------------------------------------

    async def _execute(self, statement: Any) -> "CursorResult[Any]":
        """Runs a DML statement and returns a result that has `rowcount`.

        The `cast` is a typing accommodation, not a claim: `execute` is
        declared to return `Result[Any]`, which has no `rowcount` because
        a SELECT has no such notion — a DML statement always returns the
        `CursorResult` subtype that does.

        `synchronize_session=False` because nothing in this session holds
        these rows; there is no identity-map copy to keep consistent.
        """
        return cast(
            "CursorResult[Any]",
            await self._session.execute(statement.execution_options(synchronize_session=False)),
        )
