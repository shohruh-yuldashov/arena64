"""The admin notification reads and the one operation — A64-024.7.

One adapter satisfying both published ports, because they are two questions
about the same rows and a second adapter would be a second place for the
delivery vocabulary to be decoded.

## Every query is index-backed and bounded

    no filter        ORDER BY (created_at, id) DESC   ix_notification__created_at_id
    recipient        recipient_id = ?                 ix_notification__recipient_recent
    failed push      EXISTS (... status='failed')     ix_notification_push_delivery__failed

One page is **two** statements: the page, then every delivery for it in one
`IN`. Nothing loops a read.

## The keyset is spelled out, not a row comparison

Copied deliberately from `NotificationRepository.list_for`, whose comment
gives the reason: PostgreSQL supports `(a, b) < (x, y)` but does not use the
index for the mixed `DESC` ordering this table has, and the two-branch form
does.
"""

import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.modules.notifications.domain.push import supports_push
from app.modules.notifications.domain.push_delivery import (
    PushDeliveryOutcome,
    PushDeliveryStatus,
)
from app.modules.notifications.domain.record import (
    NavigationTargetType,
    NotificationCategory,
    NotificationType,
)
from app.modules.notifications.infrastructure.models import (
    NotificationModel,
    NotificationPushDeliveryModel,
    PushSubscriptionModel,
)
from app.modules.notifications.public.administration import (
    AdminDeliveryHealth,
    AdminNotificationDetail,
    AdminNotificationFilters,
    AdminNotificationPage,
    AdminNotificationRecord,
    AdminPushDelivery,
)

#: The states a retry may move a row **out of**, and there is exactly one
#: pair.
#:
#: Expressed as constants rather than inline so the guarded `UPDATE` and the
#: `is_retryable` property cannot drift: one decides what the console offers,
#: the other decides what the database permits, and they must be the same
#: sentence.
_RETRYABLE_STATUS = PushDeliveryStatus.FAILED.value
_RETRYABLE_OUTCOME = PushDeliveryOutcome.ATTEMPTS_EXHAUSTED.value


class SqlAlchemyAdministrativeNotificationDirectory:
    """`notifications`' admin surface over PostgreSQL.

    Satisfies `AdministrativeNotificationDirectory` and
    `NotificationDeliveryOperations`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_notifications(
        self, *, filters: AdminNotificationFilters, limit: int, cursor: str | None
    ) -> AdminNotificationPage:
        statement = select(NotificationModel)

        if filters.recipient_id is not None:
            statement = statement.where(NotificationModel.recipient_id == filters.recipient_id)

        if filters.failed_push_only:
            # `EXISTS` rather than a join: a notification with three failed
            # devices must appear once, and a join would return it three
            # times and break the keyset's row count.
            statement = statement.where(
                exists().where(
                    NotificationPushDeliveryModel.notification_id == NotificationModel.id,
                    NotificationPushDeliveryModel.status == _RETRYABLE_STATUS,
                )
            )

        if cursor is not None:
            after = _NotificationCursor.decode(cursor)
            statement = statement.where(
                or_(
                    NotificationModel.created_at < after.created_at,
                    and_(
                        NotificationModel.created_at == after.created_at,
                        NotificationModel.id < after.notification_id,
                    ),
                )
            )

        # Over-fetch by one instead of a `COUNT(*)`, which on this table
        # would be a scan on every page.
        rows = (
            (
                await self._session.execute(
                    statement.order_by(
                        NotificationModel.created_at.desc(), NotificationModel.id.desc()
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )

        has_more = len(rows) > limit
        page = list(rows[:limit])
        next_cursor = (
            _NotificationCursor(
                created_at=page[-1].created_at, notification_id=page[-1].id
            ).encode()
            if has_more and page
            else None
        )
        return AdminNotificationPage(
            records=[_to_record(row) for row in page], next_cursor=next_cursor
        )

    async def find_notification(self, notification_id: UUID) -> AdminNotificationDetail | None:
        row = await self._session.get(NotificationModel, notification_id)
        if row is None:
            return None
        deliveries = await self.deliveries_for([notification_id])
        return AdminNotificationDetail(
            notification=_to_record(row), deliveries=deliveries.get(notification_id, [])
        )

    async def delivery_health(self) -> AdminDeliveryHealth:
        """How many pushes are waiting for an operator — A64-024.9.

        `status = 'failed'` matches `ix_notification_push_delivery__failed`'s
        partial predicate exactly, so this reads an index holding only
        failures. The `outcome` narrowing is a filter over the handful of
        rows that index returns — and it is what keeps the number
        *actionable*: `permanent_failure` and `subscription_gone` are
        finished, and summing them in would invite an operator to act on a
        figure most of which needs no action.
        """
        total = await self._session.scalar(
            select(func.count())
            .select_from(NotificationPushDeliveryModel)
            .where(
                NotificationPushDeliveryModel.status == _RETRYABLE_STATUS,
                NotificationPushDeliveryModel.outcome == _RETRYABLE_OUTCOME,
            )
        )
        return AdminDeliveryHealth(retry_exhausted=total or 0)

    async def deliveries_for(
        self, notification_ids: Sequence[UUID]
    ) -> dict[UUID, Sequence[AdminPushDelivery]]:
        if not notification_ids:
            return {}

        # One statement, left-joined to the subscription so a delivery whose
        # device row was deleted outright still reports its own facts. An
        # inner join would silently drop exactly the rows an operator is
        # looking for.
        rows = await self._session.execute(
            select(
                NotificationPushDeliveryModel,
                PushSubscriptionModel.created_at,
                PushSubscriptionModel.last_seen_at,
                PushSubscriptionModel.revoked_at,
            )
            .outerjoin(
                PushSubscriptionModel,
                PushSubscriptionModel.id == NotificationPushDeliveryModel.subscription_id,
            )
            .where(NotificationPushDeliveryModel.notification_id.in_(set(notification_ids)))
            .order_by(NotificationPushDeliveryModel.created_at)
        )

        grouped: dict[UUID, list[AdminPushDelivery]] = {}
        for delivery, created_at, last_seen_at, revoked_at in rows:
            grouped.setdefault(delivery.notification_id, []).append(
                _to_delivery(
                    delivery,
                    subscription_created_at=created_at,
                    subscription_last_seen_at=last_seen_at,
                    subscription_revoked_at=revoked_at,
                )
            )
        return dict(grouped)

    # --- `NotificationDeliveryOperations` -----------------------------------

    async def retry_delivery(
        self, notification_id: UUID, subscription_id: UUID, *, at: datetime
    ) -> AdminPushDelivery | None:
        """Re-arms one exhausted delivery, or matches nothing.

        The `WHERE` carries the eligibility rule, so it is the **database**
        that decides — not a read the caller took a moment earlier and acted
        on. A worker settling the row, a second administrator, or a state
        that was never eligible all produce zero rows and `None`.

        `attempt_count` is untouched. The worker's cap is applied after the
        attempt it grants, so this buys exactly one more real attempt and
        the row returns to terminal by the existing mechanism.
        """
        updated = await self._session.execute(
            update(NotificationPushDeliveryModel)
            .where(
                NotificationPushDeliveryModel.notification_id == notification_id,
                NotificationPushDeliveryModel.subscription_id == subscription_id,
                NotificationPushDeliveryModel.status == _RETRYABLE_STATUS,
                NotificationPushDeliveryModel.outcome == _RETRYABLE_OUTCOME,
            )
            .values(
                status=PushDeliveryStatus.PENDING.value,
                next_attempt_at=at,
                # Cleared so the row does not carry a terminal verdict while
                # it is owed again — an operator reading it mid-flight would
                # otherwise see "exhausted" on a delivery that is queued.
                outcome=None,
            )
            .returning(NotificationPushDeliveryModel)
        )
        row = updated.scalar_one_or_none()
        if row is None:
            return None

        subscription = await self._session.get(PushSubscriptionModel, subscription_id)
        return _to_delivery(
            row,
            subscription_created_at=subscription.created_at if subscription else None,
            subscription_last_seen_at=subscription.last_seen_at if subscription else None,
            subscription_revoked_at=subscription.revoked_at if subscription else None,
        )


def _to_record(row: NotificationModel) -> AdminNotificationRecord:
    """One row as the published record, field by field.

    Never by reflection, so a column added to `NotificationModel` — a
    payload, a correlation id, anything — does not silently widen what the
    admin console can read. `payload` is absent for exactly that reason and
    is not an oversight.
    """
    type_ = NotificationType(row.type)
    return AdminNotificationRecord(
        id=row.id,
        recipient_id=row.recipient_id,
        type=type_,
        category=NotificationCategory(row.category),
        target_type=NavigationTargetType(row.target_type),
        target_ref=row.target_ref,
        source_event_id=row.source_event_id,
        created_at=row.created_at,
        read_at=row.read_at,
        push_capable=supports_push(type_),
    )


def _to_delivery(
    row: NotificationPushDeliveryModel,
    *,
    subscription_created_at: datetime | None,
    subscription_last_seen_at: datetime | None,
    subscription_revoked_at: datetime | None,
) -> AdminPushDelivery:
    return AdminPushDelivery(
        subscription_id=row.subscription_id,
        status=PushDeliveryStatus(row.status),
        outcome=PushDeliveryOutcome(row.outcome) if row.outcome else None,
        attempt_count=row.attempt_count,
        next_attempt_at=row.next_attempt_at,
        last_attempt_at=row.last_attempt_at,
        delivered_at=row.delivered_at,
        created_at=row.created_at,
        subscription_created_at=subscription_created_at,
        subscription_last_seen_at=subscription_last_seen_at,
        subscription_revoked_at=subscription_revoked_at,
    )


@dataclass(frozen=True, slots=True)
class _NotificationCursor:
    """The keyset position, as an opaque string — the shape every admin
    listing on this platform uses."""

    created_at: datetime
    notification_id: UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.notification_id}"
        return urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, cursor: str) -> "_NotificationCursor":
        padding = "=" * (-len(cursor) % 4)
        try:
            raw = urlsafe_b64decode(cursor + padding).decode()
            moment, identifier = raw.split("|", 1)
            return cls(
                created_at=datetime.fromisoformat(moment),
                notification_id=uuid.UUID(identifier),
            )
        except (ValueError, TypeError) as exc:
            raise ValidationError("That page cursor could not be read.") from exc


__all__ = ["SqlAlchemyAdministrativeNotificationDirectory"]
