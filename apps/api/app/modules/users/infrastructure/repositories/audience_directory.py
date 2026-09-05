"""The adapter behind `users.public.NotificationAudienceDirectory` — A64-027A.

Two statements, both over the primary key or a partial index, and neither
returning anything but an id. The narrowness is the design: this is the one
port on the platform that enumerates accounts, and a `SELECT *` behind it
would be a user export with a protocol in front of it.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.infrastructure.models import UserModel


class SqlAlchemyNotificationAudienceDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_eligible(self) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(UserModel)
            .where(UserModel.is_verified.is_(True), UserModel.is_active.is_(True))
        )
        return int(total or 0)

    async def page_eligible(self, *, after: UUID | None, limit: int) -> Sequence[UUID]:
        statement = (
            select(UserModel.id)
            .where(UserModel.is_verified.is_(True), UserModel.is_active.is_(True))
            .order_by(UserModel.id)
            .limit(limit)
        )
        # The keyset. Omitted on the first page rather than compared against
        # a sentinel id, because there is no id that sorts before every
        # possible uuid.
        if after is not None:
            statement = statement.where(UserModel.id > after)
        return list((await self._session.scalars(statement)).all())


__all__ = ["SqlAlchemyNotificationAudienceDirectory"]
