"""The SQLAlchemy `PasswordResetTokenRepository`.

Maps between `PasswordResetTokenModel` rows and `PasswordResetToken`
entities and does nothing else (repositories.md §4).

## Why `invalidate_active_for_user` is a Core `UPDATE`

One statement with the condition in the `WHERE` clause, rather than
loading rows, calling `consume` and flushing:

1. It is atomic against a concurrent forgot-password request. A
   read-modify-write would let two requests each load the same unused
   token and each believe it invalidated it, while the partial unique
   index rejected one of the inserts — leaving a caller holding a link
   whose predecessor is still live. On this table that predecessor is a
   working password reset.
2. `WHERE used_at IS NULL` makes it idempotent *in the database*, so the
   returned count is the honest number of tokens this call invalidated
   rather than the number it looked at.
3. It is one round trip regardless of how many tokens exist.

The cost is that this path does not go through the entity's `consume`, so
"the first consumption wins" is expressed twice — once in Python, once as
the `WHERE` clause. `tests/contract/test_password_reset_token_repository.py`
asserts the two agree.

## Why this file exists beside `verification_token_repository.py`

The two are near-identical, and factoring them into one generic adapter
was considered and rejected. What they share is *behaviour* — the expiry
rule, one-time use, "first consumption wins" — and that is genuinely
shared, on `OneTimeToken`, extracted by this task precisely so it would
not be written twice. What is left here is column mapping and a table
name, which are declarations rather than logic: a generic adapter would
have to be parameterised on both the model and the entity, would have to
satisfy two strict type checkers while doing it, and would buy about forty
lines. It would also couple two tables whose whole future is to diverge
(see `PasswordResetTokenModel` on `requested_ip` and `new_email`).
"""

import logging
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.modules.auth.domain.password_reset import PasswordResetToken
from app.modules.auth.infrastructure.models import PasswordResetTokenModel

logger = logging.getLogger(__name__)


class SqlAlchemyPasswordResetTokenRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: PasswordResetTokenModel) -> PasswordResetToken:
        return PasswordResetToken(
            id=row.id,
            user_id=row.user_id,
            token_hash=row.token_hash,
            created_at=row.created_at,
            expires_at=row.expires_at,
            used_at=row.used_at,
        )

    @staticmethod
    def _to_model(token: PasswordResetToken) -> PasswordResetTokenModel:
        return PasswordResetTokenModel(
            id=token.id,
            user_id=token.user_id,
            token_hash=token.token_hash,
            created_at=token.created_at,
            expires_at=token.expires_at,
            used_at=token.used_at,
        )

    async def create(self, token: PasswordResetToken) -> PasswordResetToken:
        row = self._to_model(token)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # Two constraints can fire here, and both mean the caller did
            # not invalidate first: the unique digest (a 256-bit collision,
            # i.e. never) and the partial unique index that keeps at most
            # one live token per user. Neither is recoverable by retrying
            # the same call.
            #
            # The message says nothing about the account. This exception
            # reaches an unauthenticated endpoint whose entire contract is
            # to reveal nothing — see `PasswordResetService.forgot_password`
            # on why it is swallowed there rather than returned.
            logger.warning("password_reset_token_conflict", extra={"user_id": str(token.user_id)})
            raise ConflictError("Could not issue a password reset token.") from error

        return self._to_domain(row)

    async def get_by_hash(self, token_hash: bytes) -> PasswordResetToken | None:
        row = await self._session.scalar(
            select(PasswordResetTokenModel).where(PasswordResetTokenModel.token_hash == token_hash)
        )
        return self._to_domain(row) if row is not None else None

    async def invalidate_active_for_user(self, user_id: UUID, *, at: datetime) -> int:
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(PasswordResetTokenModel)
                .where(
                    PasswordResetTokenModel.user_id == user_id,
                    # "First consumption wins" — see the module docstring.
                    PasswordResetTokenModel.used_at.is_(None),
                )
                # Nothing in this session holds these rows, so there is no
                # identity-map copy to keep consistent.
                .execution_options(synchronize_session=False)
                .values(used_at=at),
            ),
        )
        return result.rowcount

    async def count_active_for_user(self, user_id: UUID, *, at: datetime) -> int:
        rows = (
            await self._session.scalars(
                select(PasswordResetTokenModel).where(
                    PasswordResetTokenModel.user_id == user_id,
                    PasswordResetTokenModel.used_at.is_(None),
                    PasswordResetTokenModel.expires_at > at,
                )
            )
        ).all()
        return len(rows)
