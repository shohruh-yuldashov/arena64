"""Notification retention — A64-028.7, closing P2-7.

A64-028.1 recorded `notifications.notification` as "the only unbounded
durable table that grows with activity and has no retention policy". This
is that policy, and it covers four relations rather than one — the audit
found three more with the same shape.

## What is bounded already, and what is not

| Relation | Grows with | Bound |
| --- | --- | --- |
| `notification` | every social and game event | **none, until now** |
| `notification_email_delivery` | every email attempted | **none, until now** |
| `notification_push_delivery` | every push attempted | **none, until now** |
| `push_subscription` | devices, and never shrinks | revoked rows accumulate |
| `notification_preference` | users × categories × channels | bounded by accounts |
| `notification_broadcast` | operator action | bounded by operators |

The last two are left alone. A preference row is current state rather than
history, and deleting one would silently restore a default the user turned
off. A broadcast is an operator's record of what was sent to everybody, at
a rate of a handful a month.

## The horizons, and why these numbers

`NOTIFICATION_RETENTION_DAYS` is **90** and it is a product decision wearing
an engineering hat: it is how far back a player can scroll. It is a setting
precisely because the number is not ours to fix — a platform that decides
six months is right raises it without a migration.

`NOTIFICATION_DELIVERY_RETENTION_DAYS` is **30**. A delivery row is the
audit of *how* a notification was sent — which provider, which attempt,
which failure — and it answers "why did this person not get their email",
which is a question asked within days, not months. It carries an email
provider's message id, so it is also the row with the most third-party
detail in it and the least reason to keep.

`PUSH_SUBSCRIPTION_REVOKED_RETENTION_DAYS` is **30**. A revoked subscription
is a browser endpoint that has already been told to stop; keeping it does
nothing except hold a device identifier.

## The ordering invariant

There are **no foreign keys** between these tables — `notification_id` on a
delivery row is a soft reference. So deleting a notification does not
cascade, and a delivery row whose notification is gone is an orphan nothing
would ever clean up.

`NotificationRetentionPolicy` therefore refuses to construct unless the
delivery horizon is at or **inside** the notification horizon, and the
service deletes deliveries first within a run. That is an invariant rather
than a preference, which is why it is checked in code rather than described
in a settings file.

## Concurrency

Every delete is `WHERE created_at < horizon` over an indexed column, run in
bounded batches with `RETURNING`. Two runs racing delete disjoint sets or
one deletes nothing — the statement is idempotent by construction, and
nothing here reads a row before deciding to remove it.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports import NotificationRetentionStore
from app.modules.notifications.application.retention_metrics import (
    RETENTION_DELETIONS,
    RetentionRelation,
)
from app.platform.metrics.ports import MetricsRecorder, NullMetrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotificationRetentionPolicy:
    """The horizons and the bounds one run may spend."""

    notification_days: int
    delivery_days: int
    revoked_subscription_days: int
    batch_size: int
    max_batches: int

    def __post_init__(self) -> None:
        if self.delivery_days > self.notification_days:
            raise ValueError(
                "NOTIFICATION_DELIVERY_RETENTION_DAYS must be at or inside "
                "NOTIFICATION_RETENTION_DAYS: a delivery row outliving its notification is "
                "an orphan nothing else removes, because there is no foreign key between them."
            )
        if self.batch_size < 1 or self.max_batches < 1:
            raise ValueError("batch_size and max_batches must be positive")


@dataclass(frozen=True, slots=True)
class NotificationRetentionResult:
    """What one run removed, per relation.

    One field per `RetentionRelation` member, and
    `tests/unit/test_notification_retention.py` asserts the two agree — a
    relation that is pruned and not counted is one whose growth is invisible
    until it is the incident.
    """

    notifications: int
    email_deliveries: int
    push_deliveries: int
    revoked_subscriptions: int

    @property
    def total(self) -> int:
        return (
            self.notifications
            + self.email_deliveries
            + self.push_deliveries
            + self.revoked_subscriptions
        )


class NotificationRetentionService:
    """Deletes notification history past its horizon, in bounded batches."""

    def __init__(
        self,
        *,
        store: NotificationRetentionStore,
        unit_of_work: UnitOfWork,
        clock: Clock,
        policy: NotificationRetentionPolicy,
        metrics: MetricsRecorder | None = None,
    ) -> None:
        self._store = store
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._policy = policy
        self._metrics: MetricsRecorder = metrics or NullMetrics()

    async def run(self) -> NotificationRetentionResult:
        """One pass. Never raises for an empty table; a run with nothing to
        do costs one short statement per relation."""
        now = self._clock.now()
        delivery_horizon = now - timedelta(days=self._policy.delivery_days)
        notification_horizon = now - timedelta(days=self._policy.notification_days)
        subscription_horizon = now - timedelta(days=self._policy.revoked_subscription_days)

        # Deliveries first, and the order is the invariant: a notification
        # removed before its delivery rows leaves them orphaned, because
        # nothing links them at the schema level.
        email = await self._drain(
            lambda size: self._store.delete_email_deliveries(before=delivery_horizon, limit=size)
        )
        push = await self._drain(
            lambda size: self._store.delete_push_deliveries(before=delivery_horizon, limit=size)
        )
        notifications = await self._drain(
            lambda size: self._store.delete_notifications(before=notification_horizon, limit=size)
        )
        subscriptions = await self._drain(
            lambda size: self._store.delete_revoked_subscriptions(
                before=subscription_horizon, limit=size
            )
        )

        result = NotificationRetentionResult(
            notifications=notifications,
            email_deliveries=email,
            push_deliveries=push,
            revoked_subscriptions=subscriptions,
        )

        for relation, deleted in (
            (RetentionRelation.NOTIFICATION, result.notifications),
            (RetentionRelation.EMAIL_DELIVERY, result.email_deliveries),
            (RetentionRelation.PUSH_DELIVERY, result.push_deliveries),
            (RetentionRelation.REVOKED_SUBSCRIPTION, result.revoked_subscriptions),
        ):
            # Zero is recorded, not skipped: a series that disappears when a
            # relation stops being pruned is indistinguishable from one that
            # was never wired up.
            self._metrics.increment(
                RETENTION_DELETIONS, labels={"relation": relation.value}, by=deleted
            )

        logger.info(
            "notification_retention_completed",
            extra={
                "notifications": result.notifications,
                "email_deliveries": result.email_deliveries,
                "push_deliveries": result.push_deliveries,
                "revoked_subscriptions": result.revoked_subscriptions,
            },
        )
        return result

    async def _drain(self, delete_batch: Callable[[int], Awaitable[int]]) -> int:
        """Bounded batches until one comes back short, or the ceiling.

        A short batch means the horizon is caught up, which is the ordinary
        steady state. The ceiling is what stops a first run against years of
        history from being unbounded after all — the same shape
        `QueueRetentionService` uses, and for the same reason.
        """
        deleted = 0
        for _ in range(self._policy.max_batches):
            async with self._unit_of_work:
                removed = await delete_batch(self._policy.batch_size)
                await self._unit_of_work.commit()

            deleted += removed
            if removed < self._policy.batch_size:
                break
        return deleted


def notification_retention_policy(
    *,
    notification_days: int,
    delivery_days: int,
    revoked_subscription_days: int,
    batch_size: int,
    max_batches: int,
) -> NotificationRetentionPolicy:
    """A policy from `NotificationRetentionSettings`' flat integers."""
    return NotificationRetentionPolicy(
        notification_days=notification_days,
        delivery_days=delivery_days,
        revoked_subscription_days=revoked_subscription_days,
        batch_size=batch_size,
        max_batches=max_batches,
    )


__all__ = [
    "NotificationRetentionPolicy",
    "NotificationRetentionResult",
    "NotificationRetentionService",
    "notification_retention_policy",
]
