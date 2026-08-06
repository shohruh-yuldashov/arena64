"""The sinks — where a rendered notification goes.

`LoggingNotificationSink` is the terminal adapter, until there is a
transport to be terminal *into*.

A64-013.7 excludes every delivery channel: no WebSocket gateway, no push, no
email, no mobile. So the honest implementation of `NotificationSink` today
records that a notification was produced, for whom, and about what — and
stops there.

**This is a seam, not a stub.** The distinction is that everything upstream
of it is real: the event was made durable in the same transaction as its
cause, the relay claimed it, the audience was re-read, the block was
re-checked, the payload was rendered through the privacy gate. What is
missing is only the socket, and the day AD-09's gateway exists it is a second
implementation of this protocol wired in the composition root — nothing above
changes.

## What is logged, and what is emphatically not

A64-013.7: "never log sensitive payloads." So this logs **counts and
identifiers**, never the rendered profile:

    recipient_id   already the platform's standard log field
    kind           what happened, from a closed set of three
    event_id       the outbox row, for tracing

and never `subject`, which is a whole public profile — display name,
country, biography, statistics, presence. All of it is data the recipient is
entitled to see, and none of it is data a log aggregator is entitled to
retain (services.md §8.5).

`INFO`, because a delivered notification is a business event rather than
diagnostic detail — and one line per *batch* rather than per notification,
so a fan-out to a hundred friends is one line and not a hundred (CLAUDE.md
§8.8: never log inside a loop on a hot path).
"""

import logging
from collections.abc import Sequence

from app.modules.notifications.application.ports import NotificationSink
from app.modules.notifications.domain.notification import SocialNotification
from app.modules.notifications.domain.record import NotificationAnnouncement

logger = logging.getLogger(__name__)


class LoggingNotificationSink:
    """Records deliveries. The only sink until a transport exists.

    Never raises, and that is the one thing about it worth arguing: a sink
    *may* raise (see `NotificationSink`), because a real transport failing is
    something to retry. This one has nothing to fail — writing a log line
    that threw would fail an event delivery for a reason that has nothing to
    do with the delivery, and CLAUDE.md §8.10 is explicit that logging never
    changes behaviour.
    """

    async def deliver(self, notifications: list[SocialNotification]) -> None:
        if not notifications:
            return

        first = notifications[0]
        logger.info(
            "notification_delivered",
            extra={
                "event_id": str(first.event_id),
                "kind": first.kind.value,
                "recipient_count": len(notifications),
            },
        )


class NullNotificationSink:
    """Delivers nothing and records nothing.

    Wired by a test or a load run that wants the outbox exercised without
    the log volume — chosen at the composition root, since there is no
    configuration key for it and inventing one would be a switch with no
    operator who needs it. Named rather than expressed as "pass no sink", so
    that "deliveries go nowhere" is a visible choice in the wiring.
    """

    async def deliver(self, notifications: list[SocialNotification]) -> None:
        return None


class CompositeNotificationSink:
    """Fans one batch out to several sinks, in order — A64-021.1 §12.

    Two exist as of A64-021.1 and both must run: `DurableNotificationWriter`
    keeps what NT-1 promises will still be there tomorrow, and
    `LoggingNotificationSink` records that a delivery happened at all —
    including for the transient kinds the durable writer deliberately does
    not store. Replacing one with the other would have lost half of that.

    **Order is the contract, not an implementation detail.** Sinks run in the
    order they are given and the first failure propagates, so a durable sink
    placed first means a log line saying "delivered" is only ever written
    after the row exists. The reverse order would log a delivery that a
    failed write then undid.

    Not a general-purpose fan-out: there is no retry, no per-sink isolation
    and no partial success. A sink that raises fails the batch, which is
    exactly `NotificationSink`'s contract — the relay retries the event, and
    the durable writer's uniqueness makes the retry a no-op for whatever
    already landed.
    """

    def __init__(self, sinks: Sequence[NotificationSink]) -> None:
        self._sinks = tuple(sinks)

    async def deliver(self, notifications: list[SocialNotification]) -> None:
        for sink in self._sinks:
            await sink.deliver(notifications)


class NullNotificationAnnouncer:
    """Announces nothing — `NotificationAnnouncer`.

    Not a test double: it is the honest implementation for a process with
    **no fleet to announce into**. A contract suite drives the durable
    writer over a real database and no gateway, and a deployment could
    legitimately run a relay worker without one.

    Named rather than expressed as "pass no announcer", so that "there is no
    realtime here" is a visible choice in the wiring rather than an absent
    argument — the same reason `NullNotificationSink` exists beside
    `LoggingNotificationSink`.
    """

    async def announce(self, announcements: Sequence[NotificationAnnouncement]) -> None:
        return None
