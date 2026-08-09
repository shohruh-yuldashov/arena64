"""`NotificationOperationsService` — re-arming a dead push. A64-024.7.

The **only** privileged notification operation on this platform, and the
list of what it is not is longer than what it is: there is no send, no
broadcast, no compose, no recipient selection, no payload, no target, no
schedule. Everything about *what* would be delivered is already stored; this
changes only *whether the platform will try again*.

## One transaction, two writes

    async with unit_of_work:
        delivery = await deliveries.retry_delivery(...)   # guarded UPDATE
        await audit.record_administrator(...)             # A64-024.8

Both commit or neither does. A re-armed delivery with no audit entry is a
privileged action nobody can account for; an entry for a retry that rolled
back would send an incident review after a change nobody made.

## The refusal is the database's, not a read's

`retry_delivery` carries the eligibility rule in its `WHERE`. This service
does **not** read the row, decide, and then write — that would be a
time-of-check gap a worker or a second administrator could walk through.
Zero rows matched means the answer is no, whatever the reason, and the
reason is not worth distinguishing: an operator's next step is the same.

## Bounded without a new counter

The worker's attempt cap is applied *after* the attempt it grants, so an
exhausted row that is re-armed gets exactly one more real attempt and then
returns to terminal by the existing mechanism. And while it is `pending` it
is no longer eligible, so a second retry is refused until a worker has
settled it — which is what makes repeated clicking a conflict rather than a
storm.

## Preference is not overridden, and could not be

The delivery worker reads the recipient's push preference at **send** time
(`specs/notifications.md` §14). A re-armed row therefore goes through the
same check as any other, and a recipient who has since muted the category
gets `SKIPPED_PREFERENCE` rather than a push. That is not a rule this
service enforces — it is one it structurally cannot bypass, because it does
not send anything.

## Failed-attempt policy

A64-024.6's, unchanged and deliberately not re-decided: an authenticated
administrator refused by a domain safety rule writes a `FAILED` entry, in
its own transaction, because there is no mutation for it to be atomic with.
Anybody the guard rejected writes nothing here and appears in the security
log instead.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.admin.application.services.audit_recorder import AuditRecorder
from app.modules.admin.domain.audit import AuditAction, AuditOutcome, AuditSubjectType
from app.modules.admin.domain.exceptions import RetryUnavailable
from app.modules.notifications.public import (
    AdminPushDelivery,
    NotificationDeliveryOperations,
)

logger = logging.getLogger(__name__)


class NotificationOperationsService:
    """Re-arms exhausted push deliveries. It can do nothing else."""

    def __init__(
        self,
        *,
        deliveries: NotificationDeliveryOperations,
        audit: AuditRecorder,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._deliveries = deliveries
        self._audit = audit
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def retry_delivery(
        self, *, notification_id: UUID, subscription_id: UUID, actor_id: UUID
    ) -> AdminPushDelivery:
        """Re-arms one delivery, on `actor_id`'s decision.

        Raises `RetryUnavailable` when the row is not an exhausted push —
        already retried, settled by the worker in the meantime, permanently
        failed, skipped, or never eligible. Every one of those writes a
        `FAILED` audit entry, because an administrator tried something the
        platform declined.
        """
        now = self._clock.now()

        async with self._unit_of_work:
            delivery = await self._deliveries.retry_delivery(
                notification_id, subscription_id, at=now
            )
            if delivery is None:
                # Raised **inside** the block so the unit of work rolls back
                # rather than committing a transaction that changed nothing.
                # The refusal entry is written afterwards, on its own.
                raise RetryUnavailable("That delivery cannot be retried.")

            await self._audit.record_administrator(
                actor_id=actor_id,
                action=AuditAction.NOTIFICATION_DELIVERY_RETRIED,
                subject_type=AuditSubjectType.NOTIFICATION,
                subject_ref=str(notification_id),
                # Small and structured — §13. The device, what the delivery
                # was before, and how many attempts it had already spent.
                # **No payload, no endpoint, no push keys, no recipient
                # profile**: none of it would help a reviewer, and all of it
                # would be permanent.
                before={"status": "failed", "outcome": "attempts_exhausted"},
                after={
                    "status": delivery.status.value,
                    "subscription_id": str(subscription_id),
                    "attempts_already_spent": delivery.attempt_count,
                },
            )
            await self._unit_of_work.commit()

        logger.info(
            "notification_delivery_retried",
            extra={
                "notification_id": str(notification_id),
                "subscription_id": str(subscription_id),
                "attempt_count": delivery.attempt_count,
            },
        )
        return delivery

    async def record_refusal(self, *, notification_id: UUID, actor_id: UUID, refusal: str) -> None:
        """Writes the `FAILED` entry for a refused retry — A64-024.6's policy.

        Its own transaction, and correctly so: the mutation did not happen,
        so there is nothing for it to be atomic with. `refusal` is a closed
        identifier chosen by the caller, never a message and never anything
        the request supplied.
        """
        async with self._unit_of_work:
            await self._audit.record_administrator(
                actor_id=actor_id,
                action=AuditAction.NOTIFICATION_DELIVERY_RETRIED,
                subject_type=AuditSubjectType.NOTIFICATION,
                subject_ref=str(notification_id),
                outcome=AuditOutcome.FAILED,
                after={"refused": refusal},
            )
            await self._unit_of_work.commit()

        logger.warning(
            "notification_retry_refused",
            extra={
                "actor_id": str(actor_id),
                "notification_id": str(notification_id),
                "refusal": refusal,
            },
        )


__all__ = ["NotificationOperationsService"]
