"""The SQLAlchemy adapter for `application.ports.EmailDeliveryRepository`.

Database-only, per repositories.md §2. It decides *how* a delivery is
claimed and recorded, never *whether* one should be sent — that is
`EmailDeliveryService`'s, and it is settled before anything here runs.

## The claim is one statement, and that is the whole concurrency design

Two workers polling one table must not both send the same message. A
`SELECT` followed by an `UPDATE` is a race however small the window looks,
so the claim is a single `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP
LOCKED) RETURNING`, which is the same shape `platform.outbox` uses to claim
entries and `tournament` uses to claim no-show attempts.

`SKIP LOCKED` rather than `NOWAIT`: a second worker should take the *next*
rows rather than fail, which is what makes running two of them a throughput
decision instead of a coordination problem.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.application.ports import DueEmailDelivery
from app.modules.notifications.domain.email_delivery import (
    EmailDeliveryOutcome,
    EmailDeliveryStatus,
    is_retryable,
    status_for,
)
from app.modules.notifications.domain.record import NotificationType
from app.modules.notifications.infrastructure.models import NotificationEmailDeliveryModel


class SqlAlchemyEmailDeliveryRepository:
    """Constructed per unit of work with the active session
    (repositories.md §5.1) — never holds one longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, deliveries: Sequence[DueEmailDelivery], *, at: datetime) -> int:
        """Records the intent to email. One statement, whatever the batch.

        `next_attempt_at = at` rather than a delay: the notification is
        already committed by the time a worker can claim this, and making the
        first attempt wait would add latency to buy nothing.

        `ON CONFLICT DO NOTHING` on the notification's own id. A redelivered
        source event inserts no notification, so it reaches here with an
        empty list — and if it somehow did not, the constraint refuses the
        second row without this code reading first.
        """
        if not deliveries:
            return 0

        statement = insert(NotificationEmailDeliveryModel).values(
            [
                {
                    "notification_id": delivery.notification_id,
                    "recipient_id": delivery.recipient_id,
                    "notification_type": delivery.notification_type.value,
                    "status": EmailDeliveryStatus.PENDING.value,
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
                statement.on_conflict_do_nothing(constraint="pk_notification_email_delivery")
            ),
        )
        return result.rowcount

    async def claim_due(self, *, now: datetime, limit: int) -> list[DueEmailDelivery]:
        """Claims up to `limit` due deliveries. One statement.

        `attempt_count` is incremented **by the claim**, before the provider
        is called. That ordering is deliberate: a worker that died
        mid-provider-call has already spent the attempt, and counting only
        successful returns would let a request that reliably kills the worker
        be retried forever.
        """
        due = (
            select(NotificationEmailDeliveryModel.notification_id)
            .where(
                NotificationEmailDeliveryModel.status == EmailDeliveryStatus.PENDING.value,
                NotificationEmailDeliveryModel.next_attempt_at <= now,
            )
            .order_by(NotificationEmailDeliveryModel.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )

        claimed = await self._session.execute(
            update(NotificationEmailDeliveryModel)
            .where(NotificationEmailDeliveryModel.notification_id.in_(due))
            .values(
                attempt_count=NotificationEmailDeliveryModel.attempt_count + 1,
                last_attempt_at=now,
                # Cleared so a crash between this statement and `record`
                # leaves the row invisible to the claim rather than
                # immediately re-claimable by a second worker. The recovery
                # is `reclaim_stale` below.
                next_attempt_at=None,
            )
            .returning(
                NotificationEmailDeliveryModel.notification_id,
                NotificationEmailDeliveryModel.recipient_id,
                NotificationEmailDeliveryModel.notification_type,
                NotificationEmailDeliveryModel.attempt_count,
            )
        )

        return [
            DueEmailDelivery(
                notification_id=row.notification_id,
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

        A worker that died between `claim_due` and `record` left a row with
        no `next_attempt_at`, which the claim query cannot see. Without this
        the delivery is owed forever and nothing reports it — the silent
        failure a claim-based queue has instead of a lost message.

        Bounded by `last_attempt_at`, so a delivery currently in flight is
        not stolen from a healthy worker.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(NotificationEmailDeliveryModel)
                .where(
                    NotificationEmailDeliveryModel.status == EmailDeliveryStatus.PENDING.value,
                    NotificationEmailDeliveryModel.next_attempt_at.is_(None),
                    NotificationEmailDeliveryModel.last_attempt_at < before,
                )
                .values(next_attempt_at=at)
            ),
        )
        return result.rowcount

    async def record(
        self,
        notification_id: uuid.UUID,
        *,
        outcome: EmailDeliveryOutcome,
        at: datetime,
        next_attempt_at: datetime | None = None,
        provider_message_id: str | None = None,
    ) -> None:
        """Writes how one attempt ended.

        `next_attempt_at` is written only for a retryable outcome and stays
        `NULL` otherwise, so the partial index the claim reads holds exactly
        the rows still owed and a terminal row is invisible to it forever.
        """
        status = status_for(outcome)
        await self._session.execute(
            update(NotificationEmailDeliveryModel)
            .where(NotificationEmailDeliveryModel.notification_id == notification_id)
            .values(
                status=status.value,
                outcome=outcome.value,
                next_attempt_at=next_attempt_at if is_retryable(outcome) else None,
                delivered_at=at if outcome is EmailDeliveryOutcome.DELIVERED else None,
                provider_message_id=provider_message_id,
            )
        )

    async def counts_by_status(self) -> Mapping[str, int]:
        """One aggregate per status — §21.

        No recipient, no address, no notification id. An operator can learn
        that eleven deliveries are failing and cannot learn to whom, which is
        the property that makes this safe to print.
        """
        rows = (
            await self._session.execute(
                select(
                    NotificationEmailDeliveryModel.status,
                    func.count().label("total"),
                ).group_by(NotificationEmailDeliveryModel.status)
            )
        ).all()
        return {row.status: row.total for row in rows}


def _known(value: str) -> bool:
    return value in {member.value for member in NotificationType}


__all__ = ["SqlAlchemyEmailDeliveryRepository"]
