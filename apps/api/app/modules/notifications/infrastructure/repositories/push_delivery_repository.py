"""The SQLAlchemy adapter for `application.ports.PushDeliveryRepository`.

Database-only, per repositories.md §2. It decides *how* a delivery is
claimed and recorded, never *whether* one should be sent — that is
`PushDeliveryService`'s.

The email delivery adapter's shape, with the key widened to
`(notification_id, subscription_id)`: one notification is owed one push per
device, and each one is claimed, attempted and settled on its own (§9 — one
dead device must not prevent the others).

The claim is a single `UPDATE ... WHERE (a, b) IN (SELECT ... FOR UPDATE SKIP
LOCKED) RETURNING`, for the reason its twin gives: a `SELECT` followed by an
`UPDATE` is a race however small the window looks, and `SKIP LOCKED` makes
running a second worker a throughput decision rather than a coordination
problem.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.application.ports import DuePushDelivery
from app.modules.notifications.domain.push_delivery import (
    PushDeliveryOutcome,
    PushDeliveryStatus,
    is_retryable,
    status_for,
)
from app.modules.notifications.domain.record import NotificationType
from app.modules.notifications.infrastructure.models import NotificationPushDeliveryModel


class SqlAlchemyPushDeliveryRepository:
    """Constructed per unit of work with the active session
    (repositories.md §5.1) — never holds one longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, deliveries: Sequence[DuePushDelivery], *, at: datetime) -> int:
        """Records the intent to push, per device. One statement, whatever
        the batch — so a tournament round publishing to two hundred players
        with two devices each is one insert and not four hundred.

        `next_attempt_at = at` rather than a delay: the notification is
        already committed by the time a worker can claim this, and making
        the first attempt wait would add latency to buy nothing.

        `ON CONFLICT DO NOTHING` on the pair. A redelivered source event
        inserts no notification, so it reaches here with an empty list — and
        if it somehow did not, the constraint refuses the second row without
        this code reading first (§19).
        """
        if not deliveries:
            return 0

        statement = insert(NotificationPushDeliveryModel).values(
            [
                {
                    "notification_id": delivery.notification_id,
                    "subscription_id": delivery.subscription_id,
                    "recipient_id": delivery.recipient_id,
                    "notification_type": delivery.notification_type.value,
                    "status": PushDeliveryStatus.PENDING.value,
                    "outcome": None,
                    "attempt_count": 0,
                    "next_attempt_at": at,
                    "created_at": at,
                }
                for delivery in deliveries
            ]
        )
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                statement.on_conflict_do_nothing(constraint="pk_notification_push_delivery")
            ),
        )
        return result.rowcount

    async def claim_due(self, *, now: datetime, limit: int) -> list[DuePushDelivery]:
        """Claims up to `limit` due deliveries. One statement.

        `attempt_count` is incremented **by the claim**, before the push
        service is called. A worker that died mid-request has already spent
        the attempt, and counting only returns would let a request that
        reliably kills the worker be retried forever.
        """
        due = (
            select(
                NotificationPushDeliveryModel.notification_id,
                NotificationPushDeliveryModel.subscription_id,
            )
            .where(
                NotificationPushDeliveryModel.status == PushDeliveryStatus.PENDING.value,
                NotificationPushDeliveryModel.next_attempt_at <= now,
            )
            .order_by(NotificationPushDeliveryModel.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

        claimed = await self._session.execute(
            update(NotificationPushDeliveryModel)
            .where(
                tuple_(
                    NotificationPushDeliveryModel.notification_id,
                    NotificationPushDeliveryModel.subscription_id,
                ).in_(due)
            )
            .values(
                attempt_count=NotificationPushDeliveryModel.attempt_count + 1,
                last_attempt_at=now,
                # Cleared so a crash between this statement and `record`
                # leaves the row invisible to the claim rather than
                # immediately re-claimable by a second worker. The recovery
                # is `reclaim_stale` below.
                next_attempt_at=None,
            )
            .returning(
                NotificationPushDeliveryModel.notification_id,
                NotificationPushDeliveryModel.subscription_id,
                NotificationPushDeliveryModel.recipient_id,
                NotificationPushDeliveryModel.notification_type,
                NotificationPushDeliveryModel.attempt_count,
            )
        )

        return [
            DuePushDelivery(
                notification_id=row.notification_id,
                subscription_id=row.subscription_id,
                recipient_id=row.recipient_id,
                notification_type=NotificationType(row.notification_type),
                attempt_count=row.attempt_count,
            )
            for row in claimed.all()
            # A type this build no longer knows is dropped rather than
            # raising: a vocabulary can shrink, and one stale row must not
            # stop a batch. It stays `PENDING` with no `next_attempt_at`,
            # which `reclaim_stale` picks up.
            if _known(row.notification_type)
        ]

    async def reclaim_stale(self, *, before: datetime, at: datetime) -> int:
        """Returns claims that were never resolved to the pending pool.

        Bounded by `last_attempt_at`, so a delivery currently in flight is
        not stolen from a healthy worker.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(NotificationPushDeliveryModel)
                .where(
                    NotificationPushDeliveryModel.status == PushDeliveryStatus.PENDING.value,
                    NotificationPushDeliveryModel.next_attempt_at.is_(None),
                    NotificationPushDeliveryModel.last_attempt_at < before,
                )
                .values(next_attempt_at=at)
            ),
        )
        return result.rowcount

    async def record(
        self,
        notification_id: uuid.UUID,
        subscription_id: uuid.UUID,
        *,
        outcome: PushDeliveryOutcome,
        at: datetime,
        next_attempt_at: datetime | None = None,
    ) -> None:
        """Writes how one device's attempt ended.

        `next_attempt_at` is written only for a retryable outcome and stays
        `NULL` otherwise, so the partial index the claim reads holds exactly
        the rows still owed and a terminal row is invisible to it forever.
        """
        status = status_for(outcome)
        await self._session.execute(
            update(NotificationPushDeliveryModel)
            .where(
                NotificationPushDeliveryModel.notification_id == notification_id,
                NotificationPushDeliveryModel.subscription_id == subscription_id,
            )
            .values(
                status=status.value,
                outcome=outcome.value,
                next_attempt_at=next_attempt_at if is_retryable(outcome) else None,
                delivered_at=at if outcome is PushDeliveryOutcome.DELIVERED else None,
            )
        )

    async def counts_by_status(self) -> Mapping[str, int]:
        """One aggregate per status.

        No recipient, no subscription, no endpoint. An operator can learn
        that eleven pushes are failing and cannot learn to whom, which is
        the property that makes this safe to print.
        """
        rows = (
            await self._session.execute(
                select(
                    NotificationPushDeliveryModel.status,
                    func.count().label("total"),
                ).group_by(NotificationPushDeliveryModel.status)
            )
        ).all()
        return {row.status: row.total for row in rows}


def _known(value: str) -> bool:
    return value in {member.value for member in NotificationType}


__all__ = ["SqlAlchemyPushDeliveryRepository"]
