"""`notifications`' published surface — BR-2, AD-06.

Empty until A64-021.2, and that was correct: A64-021.1 built a durable
notification with exactly one consumer — its own HTTP router — and a module
with no cross-context consumer has nothing to publish. A `public` package
written in anticipation is a contract nobody has agreed to.

The consumer has arrived. `app.gateway` needs one thing: the shape of *an
announcement that a notification exists*, so it can project one onto a
socket. `NotificationAnnouncement` is published for that, and
`NotificationType` with it because the announcement carries one and a
client-facing string must come from the closed set rather than from
whatever a caller passes.

## The direction still points the right way

`notifications` does not learn that a gateway exists. It holds
`NotificationAnnouncer` — a port in the layer that needs it (AD-06) — and
the composition root supplies an implementation. What crosses this boundary
is a **value**, not a capability, and nothing here can reach a socket.

`.importlinter`'s `gateway-reaches-modules-through-public` contract already
forbids `app.gateway` from importing this module's `domain`, `application`
and `infrastructure`, so the line is enforced rather than agreed.

## What will not land here

`NotificationRecord`, its payload and its repository. A record carries an
actor snapshot — a username, a display name, an avatar key — and publishing
it would let any consumer put that on a wire. §2 of A64-021.2 is explicit
that a pushed frame carries none of it, and the surest way to keep that true
is that the transport cannot see it.

`NotificationService` likewise stays private: reading and marking are the
recipient's own actions over HTTP, and a module that could mark somebody's
notification read from inside the process is a module that will.
"""

from app.modules.notifications.domain.record import (
    NotificationAnnouncement,
    NotificationType,
)
from app.modules.notifications.public.administration import (
    AdministrativeNotificationDirectory,
    AdminNotificationDetail,
    AdminNotificationFilters,
    AdminNotificationPage,
    AdminNotificationRecord,
    AdminPushDelivery,
    NotificationDeliveryOperations,
)

__all__ = [
    "AdminNotificationDetail",
    "AdminNotificationFilters",
    "AdminNotificationPage",
    "AdminNotificationRecord",
    "AdminPushDelivery",
    "AdministrativeNotificationDirectory",
    "NotificationAnnouncement",
    "NotificationDeliveryOperations",
    "NotificationType",
]
