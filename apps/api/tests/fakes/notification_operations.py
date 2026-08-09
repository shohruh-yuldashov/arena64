"""An in-memory admin notification directory — A64-024.7.

What is faked is **storage**, never the thing under test: the route
handlers, `NotificationOperationsService` and `AdminPushDelivery.is_retryable`
all run for real against this.

## What it models, and what it deliberately does not

It models the one rule every caller's correctness rests on: a delivery is
re-armed **only** from `failed`/`attempts_exhausted`, and the transition is
decided by the storage rather than by a read the caller took first. That is
the guarded `UPDATE`'s behaviour, and modelling it is what lets a unit test
assert that a second retry conflicts.

It does **not** model the concurrency. Whether that `UPDATE` is atomic
against a worker settling the same row is PostgreSQL's, and a fake agreeing
with itself would prove nothing —
`tests/contract/test_admin_notification_operations.py` is where it can fail.

It also does not model the keyset. Ordering by `(created_at, id)` over a
list is trivially correct here and is asserted against the real index in the
contract suite.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from app.modules.notifications.domain.push_delivery import PushDeliveryStatus
from app.modules.notifications.public import (
    AdminNotificationFilters,
    AdminNotificationPage,
    AdminNotificationRecord,
    AdminPushDelivery,
)
from app.modules.notifications.public.administration import AdminNotificationDetail


class InMemoryNotificationDirectory:
    """The notification and delivery tables, as two lists.

    Satisfies `AdministrativeNotificationDirectory` and
    `NotificationDeliveryOperations`, exactly as the real adapter does.
    """

    def __init__(self) -> None:
        self.records: list[AdminNotificationRecord] = []
        self.deliveries: dict[UUID, list[AdminPushDelivery]] = {}
        self.list_calls = 0
        self.delivery_batches: list[int] = []
        """How many notifications each batch was asked about, so a caller
        that looped one read per row is visible as a count rather than only
        as a slow test."""

    def add(self, record: AdminNotificationRecord, deliveries: Sequence[AdminPushDelivery]) -> None:
        self.records.append(record)
        self.deliveries[record.id] = list(deliveries)

    async def list_notifications(
        self, *, filters: AdminNotificationFilters, limit: int, cursor: str | None
    ) -> AdminNotificationPage:
        self.list_calls += 1
        rows = sorted(self.records, key=lambda row: (row.created_at, row.id), reverse=True)

        if filters.recipient_id is not None:
            rows = [row for row in rows if row.recipient_id == filters.recipient_id]
        if filters.failed_push_only:
            rows = [
                row
                for row in rows
                if any(
                    delivery.status is PushDeliveryStatus.FAILED
                    for delivery in self.deliveries.get(row.id, ())
                )
            ]

        if cursor is not None:
            after = [index for index, row in enumerate(rows) if str(row.id) == cursor]
            rows = rows[after[0] + 1 :] if after else []

        page = rows[:limit]
        has_more = len(rows) > limit
        return AdminNotificationPage(
            records=page, next_cursor=str(page[-1].id) if has_more and page else None
        )

    async def find_notification(self, notification_id: UUID) -> AdminNotificationDetail | None:
        record = next((row for row in self.records if row.id == notification_id), None)
        if record is None:
            return None
        return AdminNotificationDetail(
            notification=record, deliveries=self.deliveries.get(notification_id, [])
        )

    async def deliveries_for(
        self, notification_ids: Sequence[UUID]
    ) -> dict[UUID, Sequence[AdminPushDelivery]]:
        self.delivery_batches.append(len(set(notification_ids)))
        wanted = set(notification_ids)
        return {key: value for key, value in self.deliveries.items() if key in wanted}

    async def retry_delivery(
        self, notification_id: UUID, subscription_id: UUID, *, at: datetime
    ) -> AdminPushDelivery | None:
        """The guarded transition, modelled as its `WHERE`.

        Matches nothing unless the row is `failed`/`attempts_exhausted`, and
        leaves `attempt_count` alone — the worker's cap is what bounds the
        attempt this grants.
        """
        rows = self.deliveries.get(notification_id, [])
        for index, delivery in enumerate(rows):
            if delivery.subscription_id != subscription_id:
                continue
            if not delivery.is_retryable:
                return None
            rearmed = AdminPushDelivery(
                subscription_id=delivery.subscription_id,
                status=PushDeliveryStatus.PENDING,
                outcome=None,
                attempt_count=delivery.attempt_count,
                next_attempt_at=at,
                last_attempt_at=delivery.last_attempt_at,
                delivered_at=delivery.delivered_at,
                created_at=delivery.created_at,
                subscription_created_at=delivery.subscription_created_at,
                subscription_last_seen_at=delivery.subscription_last_seen_at,
                subscription_revoked_at=delivery.subscription_revoked_at,
            )
            rows[index] = rearmed
            return rearmed
        return None


__all__ = ["InMemoryNotificationDirectory"]
