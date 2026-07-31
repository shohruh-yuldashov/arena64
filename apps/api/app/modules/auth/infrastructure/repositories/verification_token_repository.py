"""The SQLAlchemy `VerificationTokenRepository`.

Maps between `EmailVerificationTokenModel` rows and
`EmailVerificationToken` entities and does nothing else
(repositories.md §4).

## Why `invalidate_active_for_user` is a Core `UPDATE`

One statement with the condition in the `WHERE` clause, rather than
loading rows, calling `consume` and flushing:

1. It is atomic against a concurrent resend. A read-modify-write would
   let two resends each load the same unused token and each believe it
   invalidated it, while the partial unique index rejected one of the
   inserts — leaving a caller with a token whose predecessor is still
   live.
2. `WHERE used_at IS NULL` makes it idempotent *in the database*, so the
   returned count is the honest number of tokens this call invalidated
   rather than the number it looked at.
3. It is one round trip regardless of how many tokens exist.

The cost is that this path does not go through the entity's `consume`,
so "the first consumption wins" is expressed twice — once in Python, once
as the `WHERE` clause. `tests/contract/test_verification_token_repository.py`
asserts the two agree.
"""

import logging
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.modules.auth.domain.verification import EmailVerificationToken
from app.modules.auth.infrastructure.models import EmailVerificationTokenModel

logger = logging.getLogger(__name__)


class SqlAlchemyVerificationTokenRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: EmailVerificationTokenModel) -> EmailVerificationToken:
        return EmailVerificationToken(
            id=row.id,
            user_id=row.user_id,
            token_hash=row.token_hash,
            created_at=row.created_at,
            expires_at=row.expires_at,
            used_at=row.used_at,
        )

    @staticmethod
    def _to_model(token: EmailVerificationToken) -> EmailVerificationTokenModel:
        return EmailVerificationTokenModel(
            id=token.id,
            user_id=token.user_id,
            token_hash=token.token_hash,
            created_at=token.created_at,
            expires_at=token.expires_at,
            used_at=token.used_at,
        )

    async def create(self, token: EmailVerificationToken) -> EmailVerificationToken:
        row = self._to_model(token)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # Two constraints can fire here, and both mean the caller did
            # not invalidate first: the unique digest (a 256-bit
            # collision, i.e. never) and the partial unique index that
            # keeps at most one live token per user. Neither is
            # recoverable by retrying the same call.
            logger.warning("verification_token_conflict", extra={"user_id": str(token.user_id)})
            raise ConflictError("Could not issue a verification token.") from error

        return self._to_domain(row)

    async def get_by_hash(self, token_hash: bytes) -> EmailVerificationToken | None:
        row = await self._session.scalar(
            select(EmailVerificationTokenModel).where(
                EmailVerificationTokenModel.token_hash == token_hash
            )
        )
        return self._to_domain(row) if row is not None else None

    async def invalidate_active_for_user(self, user_id: UUID, *, at: datetime) -> int:
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(EmailVerificationTokenModel)
                .where(
                    EmailVerificationTokenModel.user_id == user_id,
                    # "First consumption wins" — see the module docstring.
                    EmailVerificationTokenModel.used_at.is_(None),
                )
                .values(used_at=at)
                # Nothing in this session holds these rows, so there is no
                # identity-map copy to keep consistent.
                .execution_options(synchronize_session=False),
            ),
        )
        return result.rowcount

    async def count_active_for_user(self, user_id: UUID, *, at: datetime) -> int:
        rows = (
            await self._session.scalars(
                select(EmailVerificationTokenModel).where(
                    EmailVerificationTokenModel.user_id == user_id,
                    EmailVerificationTokenModel.used_at.is_(None),
                    EmailVerificationTokenModel.expires_at > at,
                )
            )
        ).all()
        return len(rows)
