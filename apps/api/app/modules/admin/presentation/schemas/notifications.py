"""What the admin Notification Operations API returns — A64-024.7.

## The vocabulary is reported, never softened

`push_status` and `push_outcome` are the platform's own labels, unchanged.
In particular `sent`/`delivered` means **a push service accepted the
request** — not that a device showed anything. `domain.push_delivery` states
it plainly and this schema does not paper over it: nothing downstream of
this platform reports back, so a field named `delivered` that meant "the
person saw it" would be a claim the system cannot support.

The console renders these as localised phrases and is responsible for saying
"accepted by the push service" rather than "delivered".

## What has no field here, and therefore no serialisation path

The push endpoint, `p256dh`, `auth`, any VAPID material, any provider
response body, the notification payload, the recipient's email. A device is
described by three operational timestamps — first seen, last seen, revoked —
which answer "is this device still real" and could not be replayed anywhere.

## There is no request model for creating anything

Not an omission. There is no endpoint that composes, sends, broadcasts or
schedules a notification, so there is no shape for one. The single mutation
takes its two identifiers from the path and carries **no body at all**.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AdminPushDeliveryView(BaseModel):
    """One device's attempt at one notification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subscription_id: UUID = Field(
        description="The device, as this platform's own opaque key. Never an endpoint."
    )
    status: str = Field(description="`pending`, `sent`, `skipped` or `failed`.")
    outcome: str | None = Field(
        default=None,
        description="The bounded reason this delivery ended where it did. "
        "`null` while a row has never been attempted. Never a push service's own text.",
    )
    attempt_count: int
    next_attempt_at: datetime | None = None
    last_attempt_at: datetime | None = None
    accepted_at: datetime | None = Field(
        default=None,
        description="When a push service **accepted** the request. Not an end-device "
        "acknowledgement — this platform receives none.",
    )
    created_at: datetime

    can_retry: bool = Field(
        description="Whether an administrator may re-arm this delivery. True for exactly "
        "one state: a failed delivery whose outcome is `attempts_exhausted`."
    )

    device_first_seen_at: datetime | None = None
    device_last_seen_at: datetime | None = None
    device_revoked_at: datetime | None = Field(
        default=None,
        description="When the subscription stopped being usable — a push service "
        "answered 404/410, or the person signed out on that browser.",
    )


class AdminNotificationSummary(BaseModel):
    """One notification in the list.

    **No payload.** The stored payload is somebody's data — an actor's name,
    a tournament, a game result — and none of it answers an operational
    question. The type and the target say what the notification is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    recipient_id: UUID
    recipient_username: str | None = Field(
        default=None,
        description="Resolved per page in one batch. `None` for an account that no "
        "longer exists — the notification outlives it.",
    )
    type: str
    category: str
    created_at: datetime
    read_at: datetime | None = None
    push_capable: bool = Field(
        description="Whether this platform pushes this type at all. `false` means no "
        "push was ever owed, which is not a failure."
    )
    push_summary: str = Field(
        description="The page's one-word push standing, derived from the deliveries: "
        "`none`, `pending`, `sent`, `skipped` or `failed`."
    )
    delivery_count: int


class AdminNotificationPageResponse(BaseModel):
    """One page, and the cursor that continues it. **No total count.**"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[AdminNotificationSummary]
    next_cursor: str | None = None


class AdminNotificationDetailResponse(BaseModel):
    """One notification with every device's delivery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    recipient_id: UUID
    recipient_username: str | None = None
    type: str
    category: str
    target_type: str = Field(
        description="A closed set of internal destinations — no URL is ever stored."
    )
    target_ref: str | None = None
    source_event_id: UUID = Field(
        description="The outbox entry that caused this. Withheld from players; shown "
        "here because 'the event never fired' and 'the notification was never written' "
        "are different failures and this is what tells them apart."
    )
    created_at: datetime
    read_at: datetime | None = None
    push_capable: bool
    deliveries: list[AdminPushDeliveryView]


__all__ = [
    "AdminNotificationDetailResponse",
    "AdminNotificationPageResponse",
    "AdminNotificationSummary",
    "AdminPushDeliveryView",
]
