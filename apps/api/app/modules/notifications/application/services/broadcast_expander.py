"""Delivering a broadcast, one batch at a time — A64-027A §19.

This is the half §19 exists for. The admin request writes a row; this turns
that row into notifications, in bounded batches, on the platform's existing
`PeriodicTaskScheduler` — the same machinery the email and push workers use,
and deliberately not a second job system beside it.

## Why batches, and why a cursor

A platform-wide announcement is `O(accounts)` writes. Done in one
transaction it would hold locks for the length of the delivery and lose
everything on a restart; done with an offset it would skip and repeat
accounts as the audience changed underneath it. So: a bounded page, ordered
by primary key, resumed from the last id written.

## Why a crash is cheap

`notification_id_for` derives each recipient's `source_event_id` from the
broadcast and the player, and the notification table is unique on
`(recipient_id, source_event_id, type)`. A batch that ran, wrote rows and
died before recording its cursor is therefore replayed as a batch that
writes nothing. There is no lease to expire and no lock to reclaim — the
idempotency is a property of the data, which is the only kind that survives
a process being killed.

## Preferences are honoured, not bypassed

Every recipient goes through `NotificationDeliveryPolicy` exactly as an
event-driven notification does. §15: an administrator does not get a way
around a player's own choice, and `ANNOUNCEMENT` is deliberately absent from
`preference.LOCKED` so that the choice exists to be honoured.
"""

import logging
from typing import Final
from uuid import UUID, uuid4

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports import (
    DeliveryRequest,
    NotificationAnnouncer,
    NotificationDeliveryPolicy,
    NotificationRepository,
)
from app.modules.notifications.application.ports_broadcast import BroadcastRepository
from app.modules.notifications.domain.broadcast import (
    Broadcast,
    BroadcastAudience,
    BroadcastStatus,
    notification_id_for,
)
from app.modules.notifications.domain.preference import DeliveryChannel
from app.modules.notifications.domain.record import (
    AnnouncementSummary,
    NavigationTarget,
    NavigationTargetType,
    NotificationAnnouncement,
    NotificationCategory,
    NotificationRecord,
    NotificationType,
)
from app.modules.users.public import NotificationAudienceDirectory

logger = logging.getLogger(__name__)

#: Recipients per pass. Small enough that one transaction is short and a
#: crash costs little; large enough that a platform-wide send does not take
#: all afternoon at the scheduler's interval.
BATCH_SIZE: Final = 500


class BroadcastExpander:
    """Turns one queued broadcast into notification rows."""

    def __init__(
        self,
        *,
        broadcasts: BroadcastRepository,
        notifications: NotificationRepository,
        audience: NotificationAudienceDirectory,
        policy: NotificationDeliveryPolicy,
        announcer: NotificationAnnouncer,
        clock: Clock,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._broadcasts = broadcasts
        self._notifications = notifications
        self._audience = audience
        self._policy = policy
        self._announcer = announcer
        self._clock = clock
        self._unit_of_work = unit_of_work

    async def run_once(self) -> int:
        """One pass. Returns the number of notifications written.

        A single batch per pass rather than a loop to completion: the
        scheduler decides how often work happens, and a handler that ran
        until a platform-wide broadcast finished would be a handler that
        ignores shutdown.
        """
        async with self._unit_of_work:
            broadcast = await self._broadcasts.claim_next(now=self._clock.now())
            if broadcast is None:
                return 0

            try:
                written = await self._deliver_batch(broadcast)
                # As above: the scope does not commit for us, and a batch
                # that wrote rows and did not commit is a batch the next
                # pass repeats forever.
                await self._unit_of_work.commit()
                return written
            except Exception as error:
                # Marked failed rather than left claimed. A broadcast that
                # stayed `SENDING` after an unrecoverable error would be
                # retried forever by the next pass, and an operator reading
                # the history would see it as still in flight.
                logger.exception(
                    "broadcast_failed",
                    extra={"broadcast_id": str(broadcast.id)},
                )
                # The session is dirty from the failed batch; roll it back
                # before recording the failure, or the `UPDATE` is written
                # into a transaction that cannot commit.
                await self._unit_of_work.rollback()
                await self._broadcasts.finish(
                    broadcast.id,
                    status=BroadcastStatus.FAILED,
                    at=self._clock.now(),
                    # The type, never the message: an exception's text can
                    # carry a query, a row, or an address, and this string
                    # is rendered in a console.
                    failure_reason=type(error).__name__,
                )
                await self._unit_of_work.commit()
                return 0

    async def _deliver_batch(self, broadcast: Broadcast) -> int:
        audience_size = broadcast.audience_size
        if audience_size is None:
            audience_size = await self._count(broadcast)

        recipients = await self._next_page(broadcast)
        if not recipients:
            await self._broadcasts.finish(
                broadcast.id,
                status=BroadcastStatus.COMPLETED,
                at=self._clock.now(),
            )
            return 0

        allowed = await self._policy.permitted(
            [
                DeliveryRequest(recipient_id=player, category=NotificationCategory.ANNOUNCEMENT)
                for player in recipients
            ],
            channel=DeliveryChannel.IN_APP,
        )
        permitted = {request.recipient_id for request in allowed}

        written: list[NotificationRecord] = []
        now = self._clock.now()
        for player in recipients:
            if player not in permitted:
                continue
            record = NotificationRecord(
                id=uuid4(),
                recipient_id=player,
                type=NotificationType.PLATFORM_ANNOUNCEMENT,
                category=NotificationCategory.ANNOUNCEMENT,
                payload=AnnouncementSummary(
                    title=broadcast.title,
                    body=broadcast.body,
                    locale=broadcast.locale,
                ),
                # The closed destination set, with no identifier. An
                # administrator never supplies a URL — see
                # `NavigationTargetType`.
                target=NavigationTarget(type=NavigationTargetType.HOME),
                source_event_id=notification_id_for(broadcast.id, player),
                created_at=now,
            )
            if await self._notifications.append(record):
                written.append(record)

        await self._broadcasts.record_progress(
            broadcast.id,
            cursor=recipients[-1],
            delivered=len(written),
            audience_size=audience_size if broadcast.audience_size is None else None,
        )

        # Announced after the rows exist, so a client woken by the frame
        # finds the notification when it re-reads.
        if written:
            await self._announcer.announce(
                [NotificationAnnouncement.of(record) for record in written]
            )

        logger.info(
            "broadcast_batch_delivered",
            extra={
                "broadcast_id": str(broadcast.id),
                "considered": len(recipients),
                "written": len(written),
            },
        )
        return len(written)

    async def _count(self, broadcast: Broadcast) -> int:
        if broadcast.audience is BroadcastAudience.ALL_PLAYERS:
            return await self._audience.count_eligible()
        return len(broadcast.recipients)

    async def _next_page(self, broadcast: Broadcast) -> list[UUID]:
        """The next `BATCH_SIZE` recipients after the cursor.

        A named audience is paged the same way as a platform-wide one — by
        id, after the cursor — so the two paths resume identically and the
        cursor means one thing.
        """
        if broadcast.audience is BroadcastAudience.ALL_PLAYERS:
            return list(
                await self._audience.page_eligible(after=broadcast.cursor, limit=BATCH_SIZE)
            )

        ordered = sorted(broadcast.recipients)
        if broadcast.cursor is not None:
            ordered = [player for player in ordered if player > broadcast.cursor]
        return ordered[:BATCH_SIZE]


__all__ = ["BATCH_SIZE", "BroadcastExpander"]
