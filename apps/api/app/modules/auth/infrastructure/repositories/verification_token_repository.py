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
from app.modules.auth.domain.verification import (
    EmailVerificationToken,
    VerificationChallengeKind,
)
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
            kind=VerificationChallengeKind(row.kind),
            attempt_count=row.attempt_count,
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
            kind=token.kind.value,
            attempt_count=token.attempt_count,
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

    async def live_for_user(self, user_id: UUID, *, at: datetime) -> EmailVerificationToken | None:
        """The account's one live challenge, or `None` — A64-021.5H.

        "Live" is `used_at IS NULL` and nothing else: expiry is decided by
        the caller against its injected clock, because `now()` is not
        immutable and the partial unique index that guarantees *one* live
        row could not use it either.

        The OTP path needs this and the link path does not, and the
        asymmetry is the whole difference between the two credentials: a
        link arrives carrying its own identifier, so a lookup by digest
        finds the row. Six digits identify nothing — the *session* says who
        is verifying, and this is how the challenge is found from that.
        """
        row = await self._session.scalar(
            select(EmailVerificationTokenModel).where(
                EmailVerificationTokenModel.user_id == user_id,
                EmailVerificationTokenModel.used_at.is_(None),
            )
        )
        return None if row is None else self._to_domain(row)

    async def record_failed_attempt(self, challenge_id: UUID) -> int:
        """Counts one wrong guess. Returns the new total.

        **The database increments**, and this is the one column an attacker
        can move. A read-then-write would let two concurrent submissions
        each read four and each write five, handing out a sixth guess — the
        same race the module docstring above describes for resends, applied
        to the value that bounds a brute force.

        `RETURNING` rather than a second read, so the caller decides whether
        the limit is reached from the number this statement actually wrote.
        """
        total = await self._session.scalar(
            update(EmailVerificationTokenModel)
            .where(EmailVerificationTokenModel.id == challenge_id)
            .values(attempt_count=EmailVerificationTokenModel.attempt_count + 1)
            .returning(EmailVerificationTokenModel.attempt_count)
        )
        return int(total or 0)

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
