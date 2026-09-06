"""The bounded deletes notification retention runs — A64-028.7, closing P2-7.

Four statements of the same shape, and the shape matters more than any of
them: a `SELECT … LIMIT … FOR UPDATE SKIP LOCKED` sub-query choosing the
rows, and a `DELETE … WHERE id IN (…)` removing exactly those.

## Why not `DELETE … WHERE created_at < horizon`

Because that is one statement whose duration is proportional to how long
nobody ran it. A first run against a year of history would hold locks for
minutes, write one enormous WAL record, and — on the relation the platform
writes to most — block the writers it shares a table with. `CLAUDE.md`
§10.5's "bound everything unbounded" applies to the cleanup as much as to
the thing being cleaned.

## Why `SKIP LOCKED`

Two runs are not supposed to overlap; the scheduler runs one. But a run that
overlaps a slow predecessor must not wait on it, and it must not delete the
same rows twice — `SKIP LOCKED` gives both, and it is the same reason the
outbox pruner uses it.

## Why the predicate is always on an indexed column

`created_at` leads an index on every one of these relations, so each batch
is a bounded index scan rather than a sequential scan of a growing table.
A retention job whose cost grows with the table it is bounding is a job
that eventually cannot run.
"""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.infrastructure.models import (
    NotificationEmailDeliveryModel,
    NotificationModel,
    NotificationPushDeliveryModel,
    PushSubscriptionModel,
)


class SqlAlchemyNotificationRetentionStore:
    """`NotificationRetentionStore` over the notifications schema."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def delete_notifications(self, *, before: datetime, limit: int) -> int:
        doomed = (
            select(NotificationModel.id)
            .where(NotificationModel.created_at < before)
            .order_by(NotificationModel.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return await self._delete(NotificationModel, NotificationModel.id, doomed)

    async def delete_email_deliveries(self, *, before: datetime, limit: int) -> int:
        doomed = (
            select(NotificationEmailDeliveryModel.notification_id)
            .where(NotificationEmailDeliveryModel.created_at < before)
            .order_by(NotificationEmailDeliveryModel.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return await self._delete(
            NotificationEmailDeliveryModel,
            NotificationEmailDeliveryModel.notification_id,
            doomed,
        )

    async def delete_push_deliveries(self, *, before: datetime, limit: int) -> int:
        # A composite key — `(notification_id, subscription_id)` — so the
        # sub-query selects the pair and the delete matches on the tuple.
        # A single-column `IN` over `notification_id` alone would remove
        # every device's row for a notification when only one was chosen.
        doomed = (
            select(
                NotificationPushDeliveryModel.notification_id,
                NotificationPushDeliveryModel.subscription_id,
            )
            .where(NotificationPushDeliveryModel.created_at < before)
            .order_by(NotificationPushDeliveryModel.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(NotificationPushDeliveryModel).where(
                    tuple_(
                        NotificationPushDeliveryModel.notification_id,
                        NotificationPushDeliveryModel.subscription_id,
                    ).in_(doomed)
                )
            ),
        )
        return int(result.rowcount)

    async def delete_revoked_subscriptions(self, *, before: datetime, limit: int) -> int:
        # `revoked_at` rather than `created_at`: a subscription's age says
        # nothing about whether it is still wanted, and deleting a live one
        # would silently stop a player's push notifications. Measuring from
        # creation is the mutation the contract test exists to catch.
        #
        # The `IS NOT NULL` is redundant with the comparison — `NULL <
        # before` is unknown, so a live row is excluded either way — and it
        # is kept because the predicate should say what it means rather than
        # rely on three-valued logic to say it.
        doomed = (
            select(PushSubscriptionModel.id)
            .where(
                PushSubscriptionModel.revoked_at.is_not(None),
                PushSubscriptionModel.revoked_at < before,
            )
            .order_by(PushSubscriptionModel.revoked_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return await self._delete(PushSubscriptionModel, PushSubscriptionModel.id, doomed)

    async def _delete(self, model: Any, key: Any, doomed: Any) -> int:
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(delete(model).where(key.in_(doomed.scalar_subquery()))),
        )
        return int(result.rowcount)


__all__ = ["SqlAlchemyNotificationRetentionStore"]
