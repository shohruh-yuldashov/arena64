"""`DurableNotificationWriter` — the sink that makes NT-1 true.

A64-013.7 built the whole social pipeline and stopped at a log line, and
`domain/notification.py` said what would come next: *"when NT-1's history
arrives, a second sink persists `Notification` rows. Neither needs this type
to change."* This is that sink, and nothing above it did change — the
dispatcher still resolves the audience, re-reads the block list and renders
through the privacy gate, and now hands the result to something that keeps
it.

## Why this is a sink rather than a second consumer

A second outbox consumer would have to resolve the audience again, re-read
the social graph again and render the profile again — three reads duplicated
so that two consumers could disagree about who may be told. Implementing
`NotificationSink` reuses the one place that already got all three right.

## Preferences decide whether a durable notification exists at all

A64-021.3. Before a row is written, `NotificationDeliveryPolicy` is asked
whether this recipient still wants this category in the app. A "no" means
nothing is created — see `deliver` on why suppression belongs before the
write rather than after it.

## Not every kind is durable, and that is a rule rather than a filter

`friend_online` and `friend_offline` are **transient**. A friend coming
online is a signal to a client that is watching; a row per transition would
put thousands of entries a day into a list whose value is that it is short,
and nobody has ever wanted to read "your friend came online" from four days
ago. `DURABLE_TYPES` is the mapping, absence means transient, and the sink
delivers nothing for a kind it does not map.

## Where the snapshot comes from

`SocialNotification.subject` is a `PublicProfile` already composed for this
recipient's relationship, so the actor summary stored below cannot contain a
field the actor withheld. It is a **snapshot**: the name the actor had when
the notification was written. Re-resolving it at read time would cost one
profile lookup per row — the N+1 §31 forbids — and would rewrite history
every time somebody changed their display name.

## Transaction, and where the announcement sits relative to it

One unit of work per batch. The notification row and the uniqueness that
makes it exactly-once are the same row, so they commit atomically by
construction (§13); there is no second table to keep in step.

The relay's own ledger commits separately and afterwards, which is the right
way round: a crash between the two redelivers the event, and the redelivery
finds the row already there and does nothing.

**A64-021.2 adds one line after the commit**, and its position is the
contract: the realtime announcement is published once the row is durable,
carrying only the records this call actually inserted. See `deliver`.

## Two entry points, one path — A64-021.4

`deliver` is `NotificationSink`, and takes the social pipeline's rendered
`SocialNotification`. `store` takes records a producer composed itself,
which is what the tournament and game dispatchers hand over. The first is
a four-line adapter onto the second, so preference suppression, the
transaction boundary, the log line and the announcement exist exactly once
however a notification arrives.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

from app.core.identifiers import generate_uuid7
from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports import (
    DeliveryRequest,
    EmailDeliveryRepository,
    NotificationAnnouncer,
    NotificationDeliveryPolicy,
    NotificationRepository,
)
from app.modules.notifications.application.services.email_delivery_service import (
    deliveries_for,
)
from app.modules.notifications.domain.notification import NotificationKind, SocialNotification
from app.modules.notifications.domain.preference import ChannelAvailability, DeliveryChannel
from app.modules.notifications.domain.record import (
    CATEGORY_OF,
    ActorSummary,
    NavigationTarget,
    NavigationTargetType,
    NotificationAnnouncement,
    NotificationRecord,
    NotificationType,
)

logger = logging.getLogger(__name__)

#: Which transient kinds become durable notifications, and as what.
#:
#: A kind absent from this mapping is delivered to whatever other sinks
#: exist and stored nowhere — see this module's docstring on presence.
DURABLE_TYPES: Final[Mapping[NotificationKind, NotificationType]] = {
    NotificationKind.FRIEND_REQUEST_RECEIVED: NotificationType.FRIEND_REQUEST_RECEIVED,
    NotificationKind.FRIEND_REQUEST_ACCEPTED: NotificationType.FRIEND_REQUEST_ACCEPTED,
}


class DurableNotificationWriter:
    """Persists the notifications a recipient should still find tomorrow.

    Holds a repository port and a unit of work, and nothing else — no clock
    (every instant is on the notification) and no profile reader (the
    subject arrives rendered).
    """

    def __init__(
        self,
        *,
        notifications: NotificationRepository,
        unit_of_work: UnitOfWork,
        announcer: NotificationAnnouncer,
        policy: NotificationDeliveryPolicy,
        deliveries: EmailDeliveryRepository,
        availability: ChannelAvailability,
    ) -> None:
        self._notifications = notifications
        self._unit_of_work = unit_of_work
        self._announcer = announcer
        self._policy = policy
        self._deliveries = deliveries
        self._availability = availability

    async def deliver(self, notifications: list[SocialNotification]) -> None:
        """Stores a batch of **social** notifications — `NotificationSink`.

        An empty batch, or one that is entirely transient, commits nothing.

        **Raises on failure**, which is `NotificationSink`'s contract and
        the correct choice here: a notification that could not be stored is
        one NT-1 promised would exist, so the dispatcher records the event as
        failed and the relay retries it. Swallowing it would mark the event
        published and lose the record silently.
        """
        await self.store(
            [
                record
                for notification in notifications
                if (record := self._record_for(notification)) is not None
            ]
        )

    async def store(self, records: Sequence[NotificationRecord]) -> None:
        """Stores records a producer composed itself — A64-021.4 §11.

        The entry point for the tournament and game dispatchers, which have
        no `SocialNotification` to hand over: their payload is a tournament
        or a result, not a rendered profile, and forcing them through
        `deliver` would mean inventing a social shape to carry a fact that
        is not social.

        **`deliver` is now a thin adapter onto this**, which is the property
        that matters: preference enforcement, the transaction, the log line
        and the realtime announcement are written once and every producer
        gets the same ones. A dispatcher that wrote its own rows would be a
        second place §10's suppression could be forgotten.
        """
        durable = list(records)
        if not durable:
            return

        # **A64-021.3 §10 — the preference, asked now.**
        #
        # Before anything is written, not after: a muted category produces no
        # row, so there is no unread count to move, no frame to send, and
        # nothing for a later change of mind to reveal. Writing and then
        # filtering on read would leave a record the player never consented
        # to in a table they cannot see, and the first reporting query would
        # find it.
        #
        # Asked **here** rather than when the event was written, for the
        # reason the audience and the privacy gate are re-read at delivery:
        # somebody who mutes a category between the friend request and the
        # relay tick that carries it has muted it.
        #
        # One call for the whole batch — the policy takes a sequence
        # precisely so a tournament fan-out is one query rather than one per
        # entrant (§11).
        allowed = await self._policy.permitted(
            [DeliveryRequest(recipient_id=r.recipient_id, category=r.category) for r in durable],
            channel=DeliveryChannel.IN_APP,
        )
        durable = [
            record
            for record in durable
            if DeliveryRequest(recipient_id=record.recipient_id, category=record.category)
            in allowed
        ]
        if not durable:
            return

        stored: list[NotificationRecord] = []
        async with self._unit_of_work:
            for record in durable:
                if await self._notifications.append(record):
                    stored.append(record)

            # **A64-021.5 §8 — the email is owed in the same transaction.**
            #
            # Only for rows this call actually inserted, so a redelivered
            # event queues nothing, and *inside* the unit of work, so the
            # intent to email is exactly as durable as the record it
            # describes. Enqueueing after the commit would leave a window in
            # which a crash produced a notification nobody would ever be
            # emailed about — and nothing would record that one was owed.
            #
            # No provider is called here and no address is read. This writes
            # a row saying "somebody owes this an email"; the worker decides
            # whether to send it, minutes later, against the preference and
            # the address as they are *then* (§7).
            if self._availability.can_deliver(DeliveryChannel.EMAIL):
                await self._deliveries.enqueue(deliveries_for(stored), at=_queued_at(stored))
            await self._unit_of_work.commit()

        logger.info(
            "notification_stored",
            extra={
                # Ids and counts only. `subject` is a whole public profile
                # and never reaches a log line — A64-013.7's rule, and
                # `LoggingNotificationSink` documents why.
                "event_id": str(durable[0].source_event_id),
                "type": durable[0].type.value,
                "written": len(stored),
                "duplicates": len(durable) - len(stored),
            },
        )

        # **After the commit, and only what was written** — A64-021.2 §1,
        # §5, and A64-021.1 §13's "do not emit realtime delivery before
        # durable persistence commits".
        #
        # Two properties fall out of those two words:
        #
        #   *after*  a client woken by this frame reads `GET /notifications`
        #            and finds the row. Announcing inside the transaction
        #            would let a fast client read before the commit landed
        #            and conclude nothing was there
        #   *only*   a redelivered event inserts nothing, so it announces
        #            nothing. The duplicate suppression a client also does
        #            is a second line rather than the only one
        #
        # The announcer never raises (`NotificationAnnouncer`), so nothing
        # below can undo the write above or fail the relay tick.
        await self._announcer.announce([NotificationAnnouncement.of(r) for r in stored])

    def _record_for(self, notification: SocialNotification) -> NotificationRecord | None:
        """One transient notification as a durable row, or `None` if its kind
        is not one this platform keeps."""
        type_ = DURABLE_TYPES.get(notification.kind)
        if type_ is None:
            return None

        identity = notification.subject.identity
        return NotificationRecord(
            id=generate_uuid7(),
            recipient_id=notification.recipient_id,
            type=type_,
            category=CATEGORY_OF[type_],
            payload=ActorSummary(
                player_id=identity.id,
                username=identity.username,
                display_name=identity.display_name,
                avatar_object_key=identity.avatar.object_key,
                avatar_version=identity.avatar.version,
            ),
            target=_target_for(type_, identity.username),
            source_event_id=notification.event_id,
            created_at=notification.occurred_at,
        )


def _target_for(type_: NotificationType, actor_username: str) -> NavigationTarget:
    """Where tapping this notification takes the recipient — §6.

    A received request goes to the list where it can be answered, not to the
    sender's profile: the action the recipient wants is accept or decline,
    and a profile page is one navigation short of it.

    An acceptance goes to the new friend's profile, because there is nothing
    left to answer — the interesting thing is the person.
    """
    if type_ is NotificationType.FRIEND_REQUEST_RECEIVED:
        return NavigationTarget(type=NavigationTargetType.FRIEND_REQUESTS)
    return NavigationTarget(type=NavigationTargetType.PLAYER_PROFILE, ref=actor_username)


def _queued_at(stored: Sequence[NotificationRecord]) -> datetime:
    """When the email became owed.

    The **newest** notification's own instant, not `now`. This service holds
    no clock by design — every instant it needs is on a record — and the
    alternative would be injecting one for a value whose only use is
    ordering a queue.

    A relay catching up after an outage therefore enqueues with the events'
    own instants, which are in the past, so every one of them is immediately
    due. That is correct: they were owed then.
    """
    return max(record.created_at for record in stored)


__all__ = ["DURABLE_TYPES", "DurableNotificationWriter"]
