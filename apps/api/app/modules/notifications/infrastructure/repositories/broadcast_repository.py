"""The adapter behind `BroadcastRepository` — A64-027A §19, §20."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.domain.broadcast import (
    Broadcast,
    BroadcastAudience,
    BroadcastChannel,
    BroadcastStatus,
)
from app.modules.notifications.infrastructure.models import (
    BROADCAST_IDEMPOTENCY_UNIQUE,
    NotificationBroadcastModel,
)


def _to_domain(row: NotificationBroadcastModel) -> Broadcast:
    return Broadcast(
        id=row.id,
        title=row.title,
        body=row.body,
        locale=row.locale,
        audience=BroadcastAudience(row.audience),
        channel=BroadcastChannel(row.channel),
        status=BroadcastStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
        idempotency_key=row.idempotency_key,
        recipients=tuple(UUID(str(value)) for value in row.recipients),
        audience_size=row.audience_size,
        delivered=row.delivered,
        started_at=row.started_at,
        completed_at=row.completed_at,
        failure_reason=row.failure_reason,
        cursor=row.cursor,
    )


class SqlAlchemyBroadcastRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, broadcast: Broadcast) -> Broadcast:
        """`INSERT ... ON CONFLICT DO NOTHING`, then read back.

        The read-back is not a wasted round trip: on a conflict the insert
        returns nothing, and the row the administrator should be given is
        the one that already exists. Doing this as an `ON CONFLICT DO
        UPDATE` would let a second submission overwrite the text of a
        broadcast that may already be delivering.
        """
        statement = (
            insert(NotificationBroadcastModel)
            .values(
                id=broadcast.id,
                title=broadcast.title,
                body=broadcast.body,
                locale=broadcast.locale,
                audience=broadcast.audience.value,
                channel=broadcast.channel.value,
                status=broadcast.status.value,
                created_by=broadcast.created_by,
                idempotency_key=broadcast.idempotency_key,
                recipients=[str(value) for value in broadcast.recipients],
                audience_size=broadcast.audience_size,
                delivered=broadcast.delivered,
                created_at=broadcast.created_at,
            )
            .on_conflict_do_nothing(constraint=BROADCAST_IDEMPOTENCY_UNIQUE)
        )
        await self._session.execute(statement)

        existing = await self._session.scalar(
            select(NotificationBroadcastModel).where(
                NotificationBroadcastModel.created_by == broadcast.created_by,
                NotificationBroadcastModel.idempotency_key == broadcast.idempotency_key,
            )
        )
        # Unreachable: the insert either wrote the row or conflicted with
        # one. Kept because a silent `None` here would become a 500 with no
        # explanation at the one moment an administrator is sending.
        if existing is None:  # pragma: no cover
            raise RuntimeError("broadcast vanished between insert and read")
        return _to_domain(existing)

    async def claim_next(self, *, now: datetime) -> Broadcast | None:
        """`FOR UPDATE SKIP LOCKED`, so two workers take two broadcasts.

        `SENDING` rows are claimable as well as `QUEUED` ones: a worker that
        died mid-delivery left its broadcast in `SENDING`, and the cursor it
        recorded is where the next one resumes. That is safe precisely
        because a repeated batch writes nothing — see
        `domain.broadcast.notification_id_for`.
        """
        row = await self._session.scalar(
            select(NotificationBroadcastModel)
            .where(
                NotificationBroadcastModel.status.in_(
                    (BroadcastStatus.QUEUED.value, BroadcastStatus.SENDING.value)
                )
            )
            .order_by(NotificationBroadcastModel.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None

        row.status = BroadcastStatus.SENDING.value
        if row.started_at is None:
            row.started_at = now
        await self._session.flush()
        return _to_domain(row)

    async def record_progress(
        self,
        broadcast_id: UUID,
        *,
        cursor: UUID | None,
        delivered: int,
        audience_size: int | None,
    ) -> None:
        values: dict[str, object] = {
            "cursor": cursor,
            # An increment in the statement rather than a read-modify-write,
            # so a concurrent retry cannot lose a batch's count.
            "delivered": NotificationBroadcastModel.delivered + delivered,
        }
        if audience_size is not None:
            values["audience_size"] = audience_size

        await self._session.execute(
            update(NotificationBroadcastModel)
            .where(NotificationBroadcastModel.id == broadcast_id)
            .values(**values)
        )

    async def finish(
        self,
        broadcast_id: UUID,
        *,
        status: BroadcastStatus,
        at: datetime,
        failure_reason: str | None = None,
    ) -> None:
        await self._session.execute(
            update(NotificationBroadcastModel)
            .where(NotificationBroadcastModel.id == broadcast_id)
            .values(status=status.value, completed_at=at, failure_reason=failure_reason)
        )

    async def get(self, broadcast_id: UUID) -> Broadcast | None:
        row = await self._session.get(NotificationBroadcastModel, broadcast_id)
        return None if row is None else _to_domain(row)

    async def page(self, *, limit: int, before: datetime | None) -> Sequence[Broadcast]:
        statement = (
            select(NotificationBroadcastModel)
            .order_by(
                NotificationBroadcastModel.created_at.desc(),
                NotificationBroadcastModel.id.desc(),
            )
            .limit(limit)
        )
        if before is not None:
            statement = statement.where(NotificationBroadcastModel.created_at < before)
        rows = (await self._session.scalars(statement)).all()
        return [_to_domain(row) for row in rows]


__all__ = ["SqlAlchemyBroadcastRepository"]
