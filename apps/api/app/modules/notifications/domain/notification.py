"""`SocialNotification` and `NotificationKind` — what a delivery *is*.

Framework-free (architecture.md §8), and deliberately much smaller than
`domain-model.md §9.3`'s `Notification` aggregate. That aggregate has a
category, a template key, a payload, read and dismissed state, and a
`NotificationDelivery` child per channel; it is persisted, because NT-1 says
"the notification exists even if every delivery channel fails".

**None of that is built here, and the omission is the scope A64-013.7
draws.** This task's brief is notification *infrastructure* — the outbox, the
worker, the events — with every delivery channel excluded: no WebSocket, no
push, no email, no mobile. Building a persisted aggregate whose whole
justification is surviving a channel outage, for a platform that has no
channels, would be shipping the record without the thing it records.

What exists instead is the value that crosses the seam: a rendered,
permission-applied notification handed to a `NotificationSink`. When the
gateway arrives, the sink gains an implementation; when NT-1's history
arrives, a second sink persists `Notification` rows. Neither needs this type
to change.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.modules.profiles.public import PublicProfile


class NotificationKind(StrEnum):
    """What happened, from the recipient's point of view.

    Deliberately *not* the event type. `friends.friend_request_accepted` is
    a fact about the graph; `friend_request_accepted` here is a thing a
    person is told, and the two diverge as soon as one event produces
    different notifications for different recipients — which is already true
    of presence, where the same event means "your friend is online" to every
    member of an audience that the event itself does not name.

    A `StrEnum` for the reason every closed set on this platform is one: the
    stored value, the wire value and the Python member are one string.
    """

    FRIEND_REQUEST_ACCEPTED = "friend_request_accepted"
    """Someone accepted the request you sent them."""

    FRIEND_ONLINE = "friend_online"
    """A friend came online."""

    FRIEND_OFFLINE = "friend_offline"
    """A friend went offline."""


@dataclass(frozen=True, slots=True)
class SocialNotification:
    """One notification, for one recipient, ready to deliver.

    **Already rendered and already gated.** `subject` is a `PublicProfile`
    composed through `PublicProfileComposer` for this recipient's
    relationship, so a sink cannot leak a field the subject withheld — there
    is no path from here back to the unfiltered identity.

    Frozen, so a sink cannot rewrite a recipient or a subject on its way out.
    """

    event_id: UUID
    """The outbox entry this came from.

    Carried so that a delivery can be traced back to the fact that caused it,
    and so a future channel with its own idempotency (a push provider's
    dedupe key, for instance) has a stable identifier to use that is not the
    notification's own.
    """

    recipient_id: UUID
    kind: NotificationKind
    subject: PublicProfile
    """Who the notification is *about*, as this recipient may see them."""

    occurred_at: datetime
    """When the underlying fact happened — not when it was delivered.

    A notification that arrives late must still say when the thing it
    describes occurred, or a client renders "just now" for a friend who came
    online five minutes ago while the relay was catching up.
    """
