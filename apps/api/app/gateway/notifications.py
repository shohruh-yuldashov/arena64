"""`GatewayNotificationSink` — putting a notification on a socket.
A64-021.2 §1, §3, §5, §7.

The transport A64-021.1 left a seam for. `DurableNotificationWriter` made
the notification durable and announced it to nobody, because
`NotificationAnnouncer` had one implementation and it did nothing; this is
the real one, wired at the composition root exactly as
`GatewayPendingMatchSink` was.

**Nothing upstream changes.** The notification is still produced by a source
event, still made durable in the outbox's transaction, still resolved
against a re-read social graph, still privacy-gated, and still exactly-once
by database constraint. What this adds is that the recipient finds out now
instead of on their next read.

## Why this lives in `app/gateway/` and not in `notifications`

It needs two things: the shape of an announcement, and the fleet-wide
fan-out. Putting it in `notifications.infrastructure` would make that module
import gateway internals — a module learning that a socket exists. Putting
it here makes the gateway import `notifications.public`, which is the same
direction it already imports `matchmaking.public`, `game.public` and
`friends.public`, and which `.importlinter` enforces.

`NotificationAnnouncer` is a structural `Protocol`, so this class does not
import it: it satisfies the shape, and the composition root is where the two
meet. That is AD-06 working as intended — the port stays in the layer that
needs it, and the adapter stays where the capability is.

## Transport only

This class projects three fields and calls the broadcaster. It reads no
database, resolves no profile, applies no privacy rule and makes no decision
about who may be told — all of that happened before the row was written, and
re-deciding it here would be a second answer to a settled question.

## Delivery is an optimisation — §5, §6

Every failure mode is *tolerable by construction*, because the durable
answer is `GET /notifications`:

    nobody connected     counted, not raised. The ordinary state of a
                         player who is not looking at the app
    a socket dropped     the frame is lost and the next read recovers it
    another node         forwarded through the existing bus; the forwarder
                         there delivers it — §7, unchanged
    the publish raised   counted, not raised — see `announce`

So this **never raises**, and the announcement is published *after* the
notification is committed, so there is nothing a failure here could undo.

## Ordering and duplication

Neither is guaranteed and neither needs to be. A duplicate frame costs the
client one invalidation it would have made anyway, and a late frame cannot
reopen an unread badge because the client never trusts the frame — it
re-reads (§5).
"""

import logging
from collections.abc import Sequence

from app.gateway.delivery import RoomBroadcaster
from app.gateway.metrics import NOTIFICATION_PUSHES, NotificationPushOutcome
from app.gateway.protocol import notification_created
from app.modules.notifications.public import NotificationAnnouncement
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)


class GatewayNotificationSink:
    """Announces durable notifications over the fleet's sockets."""

    def __init__(self, *, broadcaster: RoomBroadcaster, metrics: MetricsRecorder) -> None:
        self._broadcaster = broadcaster
        self._metrics = metrics

    async def announce(self, announcements: Sequence[NotificationAnnouncement]) -> None:
        """Announces a batch. An empty batch is a legal no-op.

        **One fan-out per announcement**, not per batch, because each is
        addressed to a different recipient. A batch fan-out would need one
        frame per recipient anyway, and a shared frame would mean one
        player's notification id reaching another player's socket.

        Never raises; see this module's docstring.
        """
        for announcement in announcements:
            await self._push(announcement)

    async def _push(self, announcement: NotificationAnnouncement) -> None:
        try:
            report = await self._broadcaster.deliver(
                notification_created(
                    notification_id=announcement.notification_id,
                    type_=announcement.type.value,
                    created_at=announcement.created_at,
                ),
                # **The recipient, and nobody else** — §3. A notification
                # belongs to exactly one player: there is no room to
                # broadcast into, no second participant, and no audience.
                # `spectators` is left at its default empty tuple, and
                # `notification.created` is absent from
                # `SPECTATOR_SAFE_EVENTS` besides, so a future call site
                # that passed one would still deliver nothing.
                recipients=[announcement.recipient_id],
            )
        except Exception as exc:  # noqa: BLE001 — a push must not fail a relay tick
            self._metrics.increment(
                NOTIFICATION_PUSHES, labels={"outcome": NotificationPushOutcome.FAILED}
            )
            logger.error(
                "notification_push_failed",
                extra={
                    "notification_id": str(announcement.notification_id),
                    "error": type(exc).__name__,
                },
                exc_info=exc,
            )
            return

        if report.local > 0:
            outcome = NotificationPushOutcome.LOCAL
        elif report.remote_nodes > 0:
            outcome = NotificationPushOutcome.REMOTE
        else:
            outcome = NotificationPushOutcome.NO_CONNECTION

        self._metrics.increment(NOTIFICATION_PUSHES, labels={"outcome": outcome})

        # One line per announcement, carrying what an operator traces: which
        # notification, and whether anybody was there. **Never the
        # recipient's identity and never the type's meaning** — who is being
        # told what about whom is a social fact, and a log aggregator has
        # broader read access than the table it came from (services.md §8.5,
        # and `LoggingNotificationSink` made the same point).
        logger.info(
            "notification_pushed",
            extra={
                "notification_id": str(announcement.notification_id),
                "outcome": outcome.value,
                "local": report.local,
                "remote_nodes": report.remote_nodes,
            },
        )


__all__ = ["GatewayNotificationSink"]
