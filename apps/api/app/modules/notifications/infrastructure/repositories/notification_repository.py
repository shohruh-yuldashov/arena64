"""The SQLAlchemy adapter for `application.ports.NotificationRepository`.

Database-only, per repositories.md §2: this class decides *how* to read and
write, never *whether* somebody may. There is no privacy question here —
the recipient is half of every key, so "may this caller see it" is answered
by the shape of the query rather than by a check inside it.

## Two properties this file exists to guarantee

**Exactly-once, structurally.** `append` is `INSERT ... ON CONFLICT DO
NOTHING` against `uq_notification__recipient_source_type`, so the answer to
"has this event already produced this notification" is given by the
database under concurrency rather than by a `SELECT` that two processes can
both pass. §11 forbids check-then-insert and this is why: the failure it
prevents only ever happens when two relay ticks race, which is exactly when
nobody is watching.

**Recipient scoping, in every statement.** Every `WHERE` below names
`recipient_id`, including the two that also name a notification id. A
mark-read that filtered by id alone and checked ownership afterwards would
be one refactor away from not checking — and would still have touched the
row (§30).
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.application.read_models import (
    MarkReadOutcome,
    NotificationCursor,
    NotificationPage,
)
from app.modules.notifications.domain.record import (
    NavigationTarget,
    NavigationTargetType,
    NotificationCategory,
    NotificationRecord,
    NotificationType,
    payload_as_json,
    payload_of,
)
from app.modules.notifications.infrastructure.models import (
    NOTIFICATION_SOURCE_UNIQUE,
    NotificationModel,
)

logger = logging.getLogger(__name__)


class SqlAlchemyNotificationRepository:
    """Constructed per use case with the active session
    (repositories.md §5.1) — never holds one longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def to_domain(row: NotificationModel) -> NotificationRecord:
        """Row to value object, field by field.

        The payload is decoded **against the row's own type**, so a row
        whose JSON does not match raises `MalformedNotification` here rather
        than reaching a response as a half-rendered card. That is the one
        place a stored contract violation can still be refused.
        """
        type_ = NotificationType(row.type)
        return NotificationRecord(
            id=row.id,
            recipient_id=row.recipient_id,
            type=type_,
            category=NotificationCategory(row.category),
            payload=payload_of(type_, row.payload),
            target=NavigationTarget(type=NavigationTargetType(row.target_type), ref=row.target_ref),
            source_event_id=row.source_event_id,
            created_at=row.created_at,
            read_at=row.read_at,
        )

    async def append(self, record: NotificationRecord) -> bool:
        """`INSERT ... ON CONFLICT DO NOTHING`. `True` if a row was written.

        `returning(id)` rather than `rowcount`: the driver reports zero rows
        affected for a conflict, and so does a statement the database
        skipped for any other reason. A returned id is unambiguous — the
        insert happened, and this is the writer that made it happen.
        """
        statement = (
            insert(NotificationModel)
            .values(
                id=record.id,
                recipient_id=record.recipient_id,
                type=record.type.value,
                category=record.category.value,
                payload=payload_as_json(record.payload),
                target_type=record.target.type.value,
                target_ref=record.target.ref,
                source_event_id=record.source_event_id,
                created_at=record.created_at,
                read_at=record.read_at,
            )
            .on_conflict_do_nothing(constraint=NOTIFICATION_SOURCE_UNIQUE)
            .returning(NotificationModel.id)
        )
        inserted = (await self._session.execute(statement)).scalar_one_or_none()
        return inserted is not None

    async def list_for(
        self,
        recipient_id: UUID,
        *,
        after: NotificationCursor | None,
        limit: int,
    ) -> NotificationPage:
        """One page, newest first.

        Reads `limit + 1` rows and returns `limit`, which is how the next
        cursor is decided without a second `COUNT`: if the extra row exists
        there is more, and if it does not this is the last page. A page that
        is exactly `limit` long and also last therefore does not send the
        reader back for an empty one.
        """
        conditions = [NotificationModel.recipient_id == recipient_id]
        if after is not None:
            # The keyset predicate, spelled out rather than as a row
            # comparison: PostgreSQL supports `(a, b) < (x, y)` but it does
            # not use the index for the mixed DESC ordering this table has,
            # and the two-branch form does.
            conditions.append(
                or_(
                    NotificationModel.created_at < after.created_at,
                    and_(
                        NotificationModel.created_at == after.created_at,
                        NotificationModel.id < after.notification_id,
                    ),
                )
            )

        statement = (
            select(NotificationModel)
            .where(*conditions)
            .order_by(NotificationModel.created_at.desc(), NotificationModel.id.desc())
            .limit(limit + 1)
        )
        rows: Sequence[NotificationModel] = (await self._session.execute(statement)).scalars().all()

        has_more = len(rows) > limit
        page = rows[:limit]
        cursor = (
            NotificationCursor(created_at=page[-1].created_at, notification_id=page[-1].id)
            if has_more and page
            else None
        )
        return NotificationPage(entries=[self.to_domain(row) for row in page], next_cursor=cursor)

    async def count_unread(self, recipient_id: UUID) -> int:
        """`COUNT(*)` over the partial index. No rows are loaded — §10."""
        statement = (
            select(func.count())
            .select_from(NotificationModel)
            .where(
                NotificationModel.recipient_id == recipient_id,
                NotificationModel.read_at.is_(None),
            )
        )
        return (await self._session.execute(statement)).scalar_one()

    async def mark_read(
        self, notification_id: UUID, *, recipient_id: UUID, at: datetime
    ) -> MarkReadOutcome:
        """Sets `read_at` if it is unset, and reports which of three happened.

        Two statements rather than one, and the reason is idempotency: the
        `UPDATE` narrows to `read_at IS NULL`, so a second call changes
        nothing and would be indistinguishable from "not yours" if its own
        row count were the whole answer. The existence check that follows a
        no-op update is what tells those two apart.

        The common case — an unread notification — costs **one** statement,
        because the second only runs when the update matched nothing.
        """
        updated = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(NotificationModel)
                .where(
                    NotificationModel.id == notification_id,
                    NotificationModel.recipient_id == recipient_id,
                    NotificationModel.read_at.is_(None),
                )
                .values(read_at=at)
            ),
        )
        if updated.rowcount:
            return MarkReadOutcome.MARKED

        exists = await self._session.execute(
            select(NotificationModel.id).where(
                NotificationModel.id == notification_id,
                NotificationModel.recipient_id == recipient_id,
            )
        )
        return (
            MarkReadOutcome.ALREADY_READ
            if exists.scalar_one_or_none() is not None
            else MarkReadOutcome.NOT_FOUND
        )

    async def mark_all_read(self, recipient_id: UUID, *, at: datetime) -> int:
        """One `UPDATE` over the unread partial index. Returns rows changed."""
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(NotificationModel)
                .where(
                    NotificationModel.recipient_id == recipient_id,
                    NotificationModel.read_at.is_(None),
                )
                .values(read_at=at)
            ),
        )
        return int(result.rowcount)


__all__ = ["SqlAlchemyNotificationRepository"]
