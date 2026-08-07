"""Delivering notifications as Web Push — A64-021.6 §10, §14, §17, §18.

One pass of the worker: claim, decide, send, record. The email delivery
service's shape, and the differences are all downstream of one fact — a row
is **one device**, not one person.

    a batch may hold three rows for one recipient, one per browser
    a preference is read once per recipient, not once per row
    a `410` revokes the subscription and settles that row alone (§9)

## Why the notification is never loaded

Unlike email, which renders a subject and a body from it, a push payload is
the notification's **id and type** — both of which the delivery row already
carries. So this worker sends without reading `notification`, which makes a
fan-out to two hundred players across two devices each a claim, one
preference read, and four hundred HTTP requests. No lookup per device (§27).

## Three transactions, deliberately

The claim commits before any push service is called, each result commits
after its own, and the reclaim is its own. Holding a transaction open across
a network call means a push service that hangs for thirty seconds holds a
row lock for thirty seconds — and a batch of them holds the connection pool.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports import (
    DeliveryRequest,
    DuePushDelivery,
    NotificationDeliveryPolicy,
    PushDeliveryRepository,
    PushSubscriptionRepository,
)
from app.modules.notifications.domain.preference import ChannelAvailability, DeliveryChannel
from app.modules.notifications.domain.push import PushPayload, supports_push
from app.modules.notifications.domain.push_delivery import (
    PushDeliveryOutcome,
    next_attempt_at,
    revokes_subscription,
)
from app.modules.notifications.domain.record import CATEGORY_OF
from app.modules.notifications.domain.subscription import PushSubscription
from app.platform.metrics import MetricsRecorder
from app.platform.push import (
    PermanentPushFailure,
    PushMessage,
    PushProvider,
    PushRecipient,
    TransientPushFailure,
)

logger = logging.getLogger(__name__)

#: `{type, outcome}` — both closed enumerations this platform defines.
#:
#: §26 is explicit about what may not be a label: an endpoint, a user id, a
#: notification id, a device id. Every one of them is unbounded cardinality,
#: and three of them are fed by a third party.
NOTIFICATION_PUSH_DELIVERIES: Final = "notifications.push.deliveries"

#: How long a claim may sit unresolved before another worker may take it.
#:
#: Ten minutes, matching the email channel. It must exceed the longest a
#: single attempt can take — the transport's timeout is ten seconds — by
#: enough that a slow batch is never mistaken for a dead worker.
STALE_CLAIM_AFTER: Final = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class PushDeliveryPass:
    """What one pass did. Counts and outcomes, never recipients."""

    claimed: int = 0
    reclaimed: int = 0
    revoked: int = 0
    outcomes: dict[PushDeliveryOutcome, int] = field(default_factory=dict)

    def counted(self, outcome: PushDeliveryOutcome) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1


class PushDeliveryService:
    """One pass: claim, decide, send, record."""

    def __init__(
        self,
        *,
        deliveries: PushDeliveryRepository,
        subscriptions: PushSubscriptionRepository,
        policy: NotificationDeliveryPolicy,
        provider: PushProvider | None,
        metrics: MetricsRecorder,
        unit_of_work: UnitOfWork,
        clock: Clock,
        availability: ChannelAvailability,
        batch_size: int,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
        ttl_seconds: int,
    ) -> None:
        self._deliveries = deliveries
        self._subscriptions = subscriptions
        self._policy = policy
        # `None` when this process holds no VAPID key pair. Not an error:
        # the rows are settled as `SKIPPED_CHANNEL_UNAVAILABLE`, which is
        # true and is what an operator needs to see.
        self._provider = provider
        self._metrics = metrics
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._availability = availability
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._ttl_seconds = ttl_seconds

    async def deliver_once(self) -> PushDeliveryPass:
        """One pass. Returns what it did."""
        now = self._clock.now()

        async with self._unit_of_work:
            reclaimed = await self._deliveries.reclaim_stale(before=now - STALE_CLAIM_AFTER, at=now)
            await self._unit_of_work.commit()

        async with self._unit_of_work:
            claimed = await self._deliveries.claim_due(now=now, limit=self._batch_size)
            await self._unit_of_work.commit()

        result = PushDeliveryPass(claimed=len(claimed), reclaimed=reclaimed)
        if not claimed:
            return result

        # Two batch reads for the whole pass, before a single message is
        # encrypted. §27: a per-delivery lookup here is the N+1 that only
        # appears once a real tournament fans out across real devices.
        subscriptions = await self._subscriptions_for(claimed)
        permitted = await self._policy.permitted(
            [
                DeliveryRequest(
                    recipient_id=delivery.recipient_id,
                    category=CATEGORY_OF[delivery.notification_type],
                )
                for delivery in claimed
            ],
            channel=DeliveryChannel.PUSH,
        )

        revoked = 0
        for delivery in claimed:
            outcome = await self._attempt(delivery, subscriptions, permitted)
            result.counted(outcome)
            revoked += await self._resolve(delivery, outcome)
            self._metrics.increment(
                NOTIFICATION_PUSH_DELIVERIES,
                labels={
                    "type": delivery.notification_type.value,
                    "outcome": outcome.value,
                },
            )

        settled = PushDeliveryPass(
            claimed=result.claimed,
            reclaimed=result.reclaimed,
            revoked=revoked,
            outcomes=result.outcomes,
        )
        logger.info(
            "notification_push_pass",
            extra={
                # Counts and outcomes only. No recipient, no endpoint, no
                # subscription, no notification id — §26.
                "claimed": settled.claimed,
                "reclaimed": settled.reclaimed,
                "revoked": settled.revoked,
                "outcomes": {outcome.value: count for outcome, count in settled.outcomes.items()},
            },
        )
        return settled

    async def _subscriptions_for(
        self, claimed: Sequence[DuePushDelivery]
    ) -> Mapping[UUID, PushSubscription]:
        """Every claimed row's subscription, by id, in **one** query.

        Keyed on the **subscription** and fetched through the recipients'
        live sets, which is the one lookup the ownership rules permit: there
        is no `get(subscription_id)` on the port, deliberately (§25), so a
        subscription is only ever reached through the account that owns it.

        That also settles a subtle case for free. A subscription re-bound to
        a different account between enqueue and send is not in *this*
        recipient's set, so it falls out here and is skipped — the browser
        that changed hands is never pushed the previous owner's
        notification (§23).

        A row whose subscription is absent — revoked since enqueue, or
        re-bound to another account — is simply missing from this map and is
        settled as `SKIPPED_NO_SUBSCRIPTION`.
        """
        by_recipient = await self._subscriptions.live_for_many(
            list({delivery.recipient_id for delivery in claimed})
        )
        return {
            subscription.id: subscription
            for subscriptions in by_recipient.values()
            for subscription in subscriptions
        }

    async def _attempt(
        self,
        delivery: DuePushDelivery,
        subscriptions: Mapping[UUID, PushSubscription],
        permitted: frozenset[DeliveryRequest],
    ) -> PushDeliveryOutcome:
        """One device's delivery, decided and possibly sent.

        The checks are ordered cheapest-and-most-final first, which is also
        most-informative first: a channel this process cannot use, then a
        type this platform does not push, then a preference, then a device.
        Each answer is terminal except the last two failures, so the
        ordering decides what an operator sees when several are true.
        """
        if self._provider is None or not self._availability.can_deliver(DeliveryChannel.PUSH):
            return PushDeliveryOutcome.SKIPPED_CHANNEL_UNAVAILABLE

        if not supports_push(delivery.notification_type):
            return PushDeliveryOutcome.SKIPPED_UNSUPPORTED_TYPE

        request = DeliveryRequest(
            recipient_id=delivery.recipient_id,
            category=CATEGORY_OF[delivery.notification_type],
        )
        if request not in permitted:
            # §14: the preference is read at **delivery** time, so a player
            # who muted push after a round was published is not pushed.
            return PushDeliveryOutcome.SKIPPED_PREFERENCE

        subscription = subscriptions.get(delivery.subscription_id)
        if subscription is None:
            return PushDeliveryOutcome.SKIPPED_NO_SUBSCRIPTION

        # The provider is passed down rather than re-read from `self`, so
        # `_send` has a non-optional one by signature and needs no assertion
        # about a check its caller already made.
        return await self._send(delivery, subscription, self._provider)

    async def _send(
        self,
        delivery: DuePushDelivery,
        subscription: PushSubscription,
        provider: PushProvider,
    ) -> PushDeliveryOutcome:
        """The one place a push service is contacted.

        Outside any transaction — see the module docstring. Every failure is
        classified by the transport, which is the only thing that can read a
        status code; nothing here branches on one.
        """
        payload = PushPayload(
            notification_id=delivery.notification_id,
            type=delivery.notification_type,
        )
        message = PushMessage(
            recipient=PushRecipient(
                endpoint=subscription.endpoint,
                p256dh=subscription.p256dh,
                auth=subscription.auth,
            ),
            payload=_encode(payload),
            ttl_seconds=self._ttl_seconds,
        )

        try:
            await provider.send(message)
        except PermanentPushFailure:
            # Covers both a gone subscription and a rejection that will
            # recur. They are one outcome here because the response is the
            # same — stop, and revoke the device — and because the transport
            # already refuses to tell this layer which status code it saw.
            return PushDeliveryOutcome.SUBSCRIPTION_GONE
        except TransientPushFailure:
            if delivery.attempt_count >= self._max_attempts:
                return PushDeliveryOutcome.ATTEMPTS_EXHAUSTED
            return PushDeliveryOutcome.RETRYABLE_FAILURE

        return PushDeliveryOutcome.DELIVERED

    async def _resolve(self, delivery: DuePushDelivery, outcome: PushDeliveryOutcome) -> int:
        """Writes the result, and revokes the device when it is finished.

        Returns how many subscriptions this settled — zero or one — so the
        pass can report it without a second read.

        Both writes are in **one transaction**, which matters: a revoked
        subscription whose delivery row still said `PENDING` would be
        re-claimed forever against a device that is gone.
        """
        now = self._clock.now()
        due = (
            next_attempt_at(
                now=now,
                attempt_count=delivery.attempt_count,
                base_seconds=self._retry_base_seconds,
                max_seconds=self._retry_max_seconds,
            )
            if outcome is PushDeliveryOutcome.RETRYABLE_FAILURE
            else None
        )

        async with self._unit_of_work:
            await self._deliveries.record(
                delivery.notification_id,
                delivery.subscription_id,
                outcome=outcome,
                at=now,
                next_attempt_at=due,
            )
            revoked = 0
            if revokes_subscription(outcome):
                # §17: one dead device is cleaned up automatically. This is
                # the only path that revokes from a delivery, and it is why
                # `SUBSCRIPTION_GONE` is its own outcome rather than a
                # flavour of permanent failure.
                revoked = int(await self._subscriptions.revoke(delivery.subscription_id, at=now))
            await self._unit_of_work.commit()
        return revoked


def _encode(payload: PushPayload) -> bytes:
    """The wire form, as compact JSON.

    `separators` without spaces, because every byte counts against the 4 KB
    ceiling and a push service rejects the whole message for one over.
    """
    return json.dumps(payload.as_dict(), separators=(",", ":")).encode()


__all__ = [
    "NOTIFICATION_PUSH_DELIVERIES",
    "STALE_CLAIM_AFTER",
    "PushDeliveryPass",
    "PushDeliveryService",
]
