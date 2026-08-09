"""What an administrator may read and operate on — A64-024.7.

A **separate published port** from `NotificationReader`, for the reason
`users`, `game` and `tournament` each have one: the existing read answers a
*player's* question — my inbox, scoped to me, newest first — and an operator
investigating "why did this person not get their round pairing" has neither
the scope nor the fields.

## The one mutation, and why it is the only one

`retry_delivery` re-arms an existing push delivery row. It is not "send a
notification": there is no way through this port to create a notification,
choose a recipient, choose a type, choose a payload or choose a destination.
Everything about *what* is delivered is already stored, and this port
changes only *whether the platform will try again*.

That is the whole reason it can exist safely, and it is why the port shape
is `(notification_id, subscription_id)` rather than anything resembling a
message. See `specs/admin.md` §6.13 for the ten conditions it had to clear.

## Delivery vocabulary is reported, never translated

`status` and `outcome` cross this boundary as the platform's own bounded
labels. In particular there is **no `DELIVERED` state meaning the person saw
it** — `SENT`/`delivered` means a push service accepted the request, and
`domain.push_delivery` says so in those words. A port that renamed it would
let a console claim an acknowledgement this platform never receives.

## What deliberately cannot travel

No push endpoint, no `p256dh`, no `auth` key, no VAPID material, no provider
response body, no notification payload JSON, no email address. The
subscription is described by operational facts only — when it was first
seen, when it was last seen, whether it is revoked — which is what an
operator needs to answer "is this device still real" and is nothing that
could be replayed against a push service.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.notifications.domain.push_delivery import (
    PushDeliveryOutcome,
    PushDeliveryStatus,
)
from app.modules.notifications.domain.record import (
    NavigationTargetType,
    NotificationCategory,
    NotificationType,
)


@dataclass(frozen=True, slots=True)
class AdminPushDelivery:
    """One device's attempt at one notification.

    `subscription_id` identifies the device **opaquely**. It is the platform's
    own key, it names nothing outside this database, and it is what a retry
    addresses — an endpoint here would be a credential in a console.
    """

    subscription_id: UUID
    status: PushDeliveryStatus
    outcome: PushDeliveryOutcome | None
    """`None` while a row has never been attempted."""

    attempt_count: int
    next_attempt_at: datetime | None
    last_attempt_at: datetime | None
    delivered_at: datetime | None
    """When a push service **accepted** the request. Never an end-device
    acknowledgement — nothing downstream of this platform reports one."""

    created_at: datetime

    subscription_created_at: datetime | None
    subscription_last_seen_at: datetime | None
    subscription_revoked_at: datetime | None
    """The three operational facts about the device, and nothing else. `None`
    for all three when the subscription row has been deleted outright."""

    @property
    def is_retryable(self) -> bool:
        """Whether an administrator may re-arm this delivery.

        **Exactly one outcome qualifies.** `ATTEMPTS_EXHAUSTED` means the
        push service was unreachable or erroring for the whole retry curve —
        an outage, and the one case where trying once more is the right
        operator action.

        Everything else is refused, and each for its own reason rather than
        by omission:

        - `PERMANENT_FAILURE` — `domain.push_delivery` states it: "retrying
          is asking the same question and being told no again".
        - `SUBSCRIPTION_GONE` — the device is gone and the subscription was
          revoked by that very outcome. There is nowhere to send.
        - every `SKIPPED_*` — nothing failed. `SKIPPED_PREFERENCE` in
          particular must never grow a retry button: that would be an
          administrator overriding a person's stated choice, which
          `specs/admin.md` §6.13 forbids outright.
        - `PENDING` — already owed; the worker has it.
        - `SENT` — accepted. There is nothing to retry.
        """
        return (
            self.status is PushDeliveryStatus.FAILED
            and self.outcome is PushDeliveryOutcome.ATTEMPTS_EXHAUSTED
        )


@dataclass(frozen=True, slots=True)
class AdminNotificationRecord:
    """One durable notification, as an operator sees it.

    Stored facts only, and **no payload**. The payload is a typed projection
    of a source event — an actor's name as it was then, a tournament's name,
    a game result — and none of it helps answer an operational question,
    while all of it is somebody's data. The type and the navigation target
    are what an operator needs to say what this notification *is*.
    """

    id: UUID
    recipient_id: UUID
    type: NotificationType
    category: NotificationCategory
    target_type: NavigationTargetType
    target_ref: str | None
    source_event_id: UUID
    """The outbox entry that caused this — the durable half of exactly-once.

    Withheld from players (§16 of the notifications spec) and shown here,
    because "did the event fire" and "did the notification get written" are
    two different failures and this is the only field that tells them apart.
    """

    created_at: datetime
    read_at: datetime | None
    push_capable: bool
    """Whether this platform pushes this type at all — `PUSH_CAPABLE_TYPES`.
    Carried so a console can say "no push was ever owed" rather than showing
    an empty delivery list that looks like a fault."""


@dataclass(frozen=True, slots=True)
class AdminNotificationDetail:
    """One notification with every device's delivery.

    Bounded by the recipient's device count, so this is two statements
    rather than a page of them.
    """

    notification: AdminNotificationRecord
    deliveries: Sequence[AdminPushDelivery]


@dataclass(frozen=True, slots=True)
class AdminNotificationFilters:
    """What an operator may narrow by — **index-backed filters only**.

    `recipient_id` rides `ix_notification__recipient_recent`, whose leading
    column it is. `failed_push_only` rides
    `ix_notification_push_delivery__failed`, added for exactly this question.

    **Deliberately absent: type, category and a time range.** None has an
    index on this table, so each would be a sequential scan that gets slower
    every day the platform runs — and an operator's real starting point is
    a person or a failure, both of which are offered. Adding them is one
    index each, when a use case asks.

    **No free-text search**, and there is nothing to search: no rendered
    text is stored (notifications spec §15.2), and the payload is typed
    JSON whose shape varies by type.
    """

    recipient_id: UUID | None = None
    failed_push_only: bool = False


@dataclass(frozen=True, slots=True)
class AdminNotificationPage:
    """One page, and the cursor that continues it.

    No total count, for the reason no admin page on this platform has one:
    an operator needs "are there more", and counting this table is a scan.
    """

    records: Sequence[AdminNotificationRecord]
    next_cursor: str | None


class AdministrativeNotificationDirectory(Protocol):
    """Reads notifications and their deliveries for the admin console."""

    async def list_notifications(
        self, *, filters: AdminNotificationFilters, limit: int, cursor: str | None
    ) -> AdminNotificationPage:
        """One page, newest first, keyed on `(created_at, id)`.

        `created_at` alone is not unique — a fan-out writes many rows in one
        instant — so the `id` tiebreak is what makes the keyset total rather
        than approximately ordered.
        """
        ...

    async def find_notification(self, notification_id: UUID) -> AdminNotificationDetail | None:
        """One notification with its deliveries, or `None`.

        Unscoped by recipient, unlike the player-facing read: an operator
        starts from an id out of a log or an audit entry, not from their own
        inbox.
        """
        ...

    async def deliveries_for(
        self, notification_ids: Sequence[UUID]
    ) -> dict[UUID, Sequence[AdminPushDelivery]]:
        """Every delivery for a page of notifications, in **one** query.

        The batch a list needs: a page of fifty notifications owes up to
        several hundred deliveries, and reading them per row is the N+1 the
        rest of this console has been written to avoid.
        """
        ...


class NotificationDeliveryOperations(Protocol):
    """The one privileged mutation — A64-024.7.

    Separate from the directory for the reason `users` splits its search
    from its administration: a consumer that may read deliveries must not
    automatically gain the ability to re-arm one.
    """

    async def retry_delivery(
        self, notification_id: UUID, subscription_id: UUID, *, at: datetime
    ) -> AdminPushDelivery | None:
        """Re-arms one exhausted delivery. Returns it, or `None`.

        A **guarded** update: the row moves from `failed`/`attempts_exhausted`
        to `pending` with `next_attempt_at = at`, and the statement matches
        nothing if the row is in any other state. That is what makes it safe
        against the worker and against a second administrator — a terminal
        row is invisible to the claim query, so nothing can be mid-flight,
        and whoever loses the race changes nothing and is told so.

        **`attempt_count` is deliberately not reset.** The worker's cap is
        applied after the attempt, so an exhausted row that is re-armed gets
        exactly **one** more real attempt and then returns to terminal. The
        bound is the existing mechanism rather than a new counter, and it is
        why this operation cannot become a retry loop.

        Creates nothing. There is no second notification row, no second
        delivery row, and no change to recipient, type, payload or target.
        """
        ...


__all__ = [
    "AdminNotificationDetail",
    "AdminNotificationFilters",
    "AdminNotificationPage",
    "AdminNotificationRecord",
    "AdminPushDelivery",
    "AdministrativeNotificationDirectory",
    "NotificationDeliveryOperations",
]
