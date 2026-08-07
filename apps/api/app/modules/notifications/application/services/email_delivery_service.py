"""`EmailDeliveryService` — one pass of the notification email worker.
A64-021.5 §7, §8, §11, §29.

Claims what is due, decides what to send, sends it, and records how each one
ended. Everything about *whether* to send is asked here, at delivery time,
and nothing about it was decided when the notification was written.

## Why every check is at delivery time and not at enqueue time

A notification is enqueued in the transaction that created it, which may be
minutes before a worker reaches it. In between, a player can mute the
category, change their address, un-verify it by changing it, or deactivate
the account. Every one of those must count — so the preference, the
recipient and the channel are all read *now*, and enqueueing records only
that an email is owed.

## Email is secondary to the record, always

§2, §29. Nothing here can fail a notification, a source action or a realtime
frame: by the time this runs, all three have committed. A provider that is
down produces retryable rows and an in-app list that is already correct.

The converse also holds and is worth stating: a delivery whose *own*
transaction fails is retried, because the row was claimed in one transaction
and resolved in another. A crash in between leaves the claim unresolved,
which `reclaim_stale` returns to the pool.

## One provider call per delivery, and the batch reads are batched

A pass claims `batch_size` rows, then does **one** recipient lookup and
**one** preference lookup for the whole batch. A tournament fan-out is the
case that makes it matter: 128 deliveries would otherwise be 256 reads
before a single email is composed.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from app.core.clock import Clock
from app.core.enums import Locale
from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports import (
    DeliveryRequest,
    DueEmailDelivery,
    EmailDeliveryRepository,
    NotificationDeliveryPolicy,
    NotificationEmailRenderer,
    NotificationRepository,
)
from app.modules.notifications.domain.email import supports_email
from app.modules.notifications.domain.email_delivery import (
    EmailDeliveryOutcome,
    next_attempt_at,
)
from app.modules.notifications.domain.preference import ChannelAvailability, DeliveryChannel
from app.modules.notifications.domain.record import CATEGORY_OF, NotificationRecord
from app.modules.users.public import EmailRecipient, EmailRecipientDirectory
from app.platform.email import EmailMessage, EmailProvider, PermanentEmailFailure
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)

#: One counter, two closed labels — §22.
#:
#: Dotted and namespaced by owning context, like every metric on this
#: platform. `outcome` is `EmailDeliveryOutcome`, so "how many were skipped
#: for preference" and "how many are being retried" are the same query with a
#: different value rather than two metrics that can disagree.
NOTIFICATION_EMAIL_DELIVERIES: Final = "notifications.email.deliveries"

#: How long a claim may sit unresolved before another worker may take it.
#:
#: Generous rather than tight: a provider call has its own timeout and a
#: pass sends serially, so a healthy worker resolves a claim within seconds
#: of making it. Ten minutes is far outside that and far inside "nobody
#: noticed for an hour", which is the window this exists to close.
STALE_CLAIM_AFTER: Final = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class EmailDeliveryPass:
    """What one worker pass did — the shape an operator and a test both read.

    Counted by outcome rather than listing deliveries, for the reason every
    log line here does: a pass over a tournament fan-out touches 128
    recipients, and a result that named them would be a list of who was
    emailed sitting in a return value.
    """

    claimed: int = 0
    reclaimed: int = 0
    outcomes: dict[EmailDeliveryOutcome, int] = field(default_factory=dict)

    def counted(self, outcome: EmailDeliveryOutcome) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1


class EmailDeliveryService:
    """One pass: claim, decide, send, record."""

    def __init__(
        self,
        *,
        deliveries: EmailDeliveryRepository,
        notifications: NotificationRepository,
        recipients: EmailRecipientDirectory,
        policy: NotificationDeliveryPolicy,
        renderer: NotificationEmailRenderer,
        provider: EmailProvider,
        metrics: MetricsRecorder,
        unit_of_work: UnitOfWork,
        clock: Clock,
        availability: ChannelAvailability,
        batch_size: int,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> None:
        self._deliveries = deliveries
        self._notifications = notifications
        self._recipients = recipients
        self._policy = policy
        self._renderer = renderer
        self._provider = provider
        self._metrics = metrics
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._availability = availability
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    async def deliver_once(self) -> EmailDeliveryPass:
        """One pass. Returns what it did.

        **Three transactions, deliberately.** The claim commits before any
        provider call, each result commits after its own, and the reclaim is
        its own. §8 forbids holding a transaction open across a network call
        to a provider, and the reason is the one that bites in production: a
        provider that hangs for thirty seconds would otherwise hold a row
        lock for thirty seconds, and a batch of them holds the connection
        pool.
        """
        now = self._clock.now()
        result = EmailDeliveryPass()

        async with self._unit_of_work:
            result = EmailDeliveryPass(
                reclaimed=await self._deliveries.reclaim_stale(
                    before=now - STALE_CLAIM_AFTER, at=now
                )
            )
            await self._unit_of_work.commit()

        async with self._unit_of_work:
            claimed = await self._deliveries.claim_due(now=now, limit=self._batch_size)
            await self._unit_of_work.commit()

        if not claimed:
            return result

        result = EmailDeliveryPass(
            claimed=len(claimed), reclaimed=result.reclaimed, outcomes=result.outcomes
        )

        # Two batch reads for the whole pass, before a single message is
        # composed. §30: a per-delivery lookup here is the N+1 that only
        # appears once a real tournament fans out.
        recipients = await self._recipients.recipients_for(
            [delivery.recipient_id for delivery in claimed]
        )
        permitted = await self._policy.permitted(
            [
                DeliveryRequest(
                    recipient_id=delivery.recipient_id,
                    category=CATEGORY_OF[delivery.notification_type],
                )
                for delivery in claimed
            ],
            channel=DeliveryChannel.EMAIL,
        )

        for delivery in claimed:
            outcome, message_id = await self._attempt(delivery, recipients, permitted)
            result.counted(outcome)
            await self._resolve(delivery, outcome, message_id)
            # **Two bounded labels, and both are closed enumerations** — §22.
            # A recipient, an address, a notification id or a provider
            # message id here would be unbounded cardinality fed by a third
            # party, which is how a metrics backend falls over.
            self._metrics.increment(
                NOTIFICATION_EMAIL_DELIVERIES,
                labels={
                    "type": delivery.notification_type.value,
                    "outcome": outcome.value,
                },
            )

        logger.info(
            "notification_email_pass",
            extra={
                # Counts and outcomes only. No recipient, no address, no
                # notification id — §23.
                "claimed": result.claimed,
                "reclaimed": result.reclaimed,
                "outcomes": {outcome.value: count for outcome, count in result.outcomes.items()},
            },
        )
        return result

    async def _attempt(
        self,
        delivery: DueEmailDelivery,
        recipients: Mapping[UUID, EmailRecipient],
        permitted: frozenset[DeliveryRequest],
    ) -> tuple[EmailDeliveryOutcome, str | None]:
        """One delivery, decided and possibly sent.

        The checks are ordered cheapest-and-most-final first, which is also
        most-informative first: a channel this process cannot use, then a
        type this platform does not email, then a preference, then an
        address. Each answer is terminal except the last two failures, so
        the ordering decides what an operator sees when several are true.
        """
        if not self._availability.can_deliver(DeliveryChannel.EMAIL):
            # A row enqueued by a node with the channel on, claimed by one
            # with it off. Nothing about the delivery is wrong, so it is
            # skipped rather than failed.
            return EmailDeliveryOutcome.SKIPPED_CHANNEL_UNAVAILABLE, None

        if not supports_email(delivery.notification_type):
            return EmailDeliveryOutcome.SKIPPED_UNSUPPORTED_TYPE, None

        request = DeliveryRequest(
            recipient_id=delivery.recipient_id,
            category=CATEGORY_OF[delivery.notification_type],
        )
        if request not in permitted:
            return EmailDeliveryOutcome.SKIPPED_PREFERENCE, None

        recipient = recipients.get(delivery.recipient_id)
        if recipient is None:
            # Absent means ineligible, and the directory deliberately does
            # not say which kind — an unknown account, a missing address, an
            # unverified one and a deactivated one are one answer, because
            # distinguishing them would be an account-existence oracle.
            return EmailDeliveryOutcome.SKIPPED_NO_EMAIL, None

        record = await self._notifications.for_recipient(
            delivery.notification_id, recipient_id=delivery.recipient_id
        )
        if record is None:
            # The notification is gone. There is nothing to render and no
            # retry that would bring it back.
            return EmailDeliveryOutcome.SKIPPED_NO_EMAIL, None

        return await self._send(record, recipient)

    async def _send(
        self, record: NotificationRecord, recipient: EmailRecipient
    ) -> tuple[EmailDeliveryOutcome, str | None]:
        try:
            rendered = self._renderer.render(record, locale=Locale(recipient.locale))
        except (LookupError, KeyError):
            # A type that reached here without a template, or a locale the
            # templates do not cover. Neither is fixed by trying again.
            logger.warning(
                "notification_email_unrenderable",
                extra={"notification_type": record.type.value},
            )
            return EmailDeliveryOutcome.SKIPPED_UNSUPPORTED_TYPE, None

        try:
            message_id = await self._provider.send(
                EmailMessage(
                    to=recipient.email,
                    subject=rendered.subject,
                    text_body=rendered.text_body,
                    html_body=rendered.html_body,
                    # **The notification's own id**, which is the delivery
                    # row's key. Deterministic across retries by
                    # construction, so a provider that deduplicates on it
                    # recognises the one case this platform's table cannot
                    # cover: a request that timed out *after* it was
                    # accepted. The row is retried — correctly, since nothing
                    # knows it arrived — and the second request sends no
                    # second copy.
                    #
                    # Safe in a header: it is an identifier the recipient
                    # already holds on their own notification, not a secret.
                    idempotency_key=str(record.id),
                )
            )
        except PermanentEmailFailure:
            return EmailDeliveryOutcome.PERMANENT_FAILURE, None
        except Exception:
            # **Everything else is retryable**, including exceptions no
            # adapter classified. An unknown fault is more likely transient
            # than permanent, and the attempt limit bounds the cost of being
            # wrong — where treating it as permanent would silently drop mail
            # on the first unfamiliar error.
            #
            # `exc_info` is deliberately absent: a provider exception can
            # carry a recipient address in its message, and §23 forbids one
            # in a log. The bounded outcome is what an operator needs.
            logger.warning(
                "notification_email_provider_failed",
                extra={"notification_type": record.type.value},
            )
            return EmailDeliveryOutcome.RETRYABLE_FAILURE, None

        return EmailDeliveryOutcome.DELIVERED, message_id

    async def _resolve(
        self,
        delivery: DueEmailDelivery,
        outcome: EmailDeliveryOutcome,
        provider_message_id: str | None,
    ) -> None:
        """Writes one result, in its own transaction.

        A retryable outcome becomes `ATTEMPTS_EXHAUSTED` at the limit rather
        than being retried forever — §11. The limit is compared against the
        attempts *already spent*, which the claim incremented, so a delivery
        that has used its last attempt is terminal here rather than after one
        more.
        """
        now = self._clock.now()
        due: datetime | None = None

        if outcome is EmailDeliveryOutcome.RETRYABLE_FAILURE:
            if delivery.attempt_count >= self._max_attempts:
                outcome = EmailDeliveryOutcome.ATTEMPTS_EXHAUSTED
            else:
                due = next_attempt_at(
                    now=now,
                    attempt_count=delivery.attempt_count,
                    base_seconds=self._retry_base_seconds,
                    max_seconds=self._retry_max_seconds,
                )

        async with self._unit_of_work:
            await self._deliveries.record(
                delivery.notification_id,
                outcome=outcome,
                at=now,
                next_attempt_at=due,
                provider_message_id=provider_message_id,
            )
            await self._unit_of_work.commit()


def deliveries_for(records: Sequence[NotificationRecord]) -> list[DueEmailDelivery]:
    """Which of these notifications are owed an email — §8's enqueue half.

    Filtered by type **here**, at enqueue time, as well as at send time. That
    is not the same check twice for the same reason: this one keeps rows that
    could never be sent out of the table entirely, where the one in
    `_attempt` catches a type removed from the capable set after its rows
    were written.

    The preference is deliberately **not** checked here. §7 requires it at
    delivery time, and a player who mutes tournament email after a round is
    published must not receive it — which only holds if the row exists and
    the send-time check refuses it.
    """
    return [
        DueEmailDelivery(
            notification_id=record.id,
            recipient_id=record.recipient_id,
            notification_type=record.type,
            attempt_count=0,
        )
        for record in records
        if supports_email(record.type)
    ]


__all__ = [
    "STALE_CLAIM_AFTER",
    "EmailDeliveryPass",
    "EmailDeliveryService",
    "deliveries_for",
]
