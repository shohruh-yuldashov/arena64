"""`GatewayForwarder` — delivering what another node published for this one.
A64-016.8, closing A64-016.5 §9.

    XADD by node A  ->  gwbus:v1:<node B>  ->  [ this loop ]  ->  B's sockets

## The gap this closes, stated plainly

A64-016.5 built both halves of the cross-node transport and left them
unconnected: `BusRemoteNodePublisher` writes to the addressee's stream and
`RedisStreamGatewayBus.consume` reads it, but **nothing called `consume`**.
So on a multi-node deployment a frame was published, reported delivered by
`REMOTE_PUBLISHES`, trimmed by `maxlen`, and never seen — and the symptom is
a player whose opponent's moves stop arriving, on a system whose metrics all
look healthy.

That is why this exists and why it is a periodic task rather than a clever
one: the frames are already correct, the routing is already correct, and what
was missing is somebody reading the mailbox.

## Why a scheduled pass rather than a blocking read

`XREADGROUP BLOCK` is the obvious shape and is wrong here for the reason
AD-20 separates workers at all: a blocking read owns a connection for its
whole duration, so a node holding forty thousand sockets would hold a Redis
connection permanently parked on a `BLOCK` — and the failure mode when that
connection drops is a node that silently stops receiving, which is the exact
defect this module was written to fix.

A bounded pass on a short interval has a worst case of one interval of added
latency and no state to lose. `GATEWAY_FORWARDING_INTERVAL_SECONDS` is
therefore small (sub-second by default) and the batch is bounded, so a node
returning from a pause drains at a rate the sockets can absorb instead of
in one burst.

## Redelivery is free, loss is not

`consume` acknowledges before returning, so an entry this process read and
then failed to deliver is gone. That is deliberate and safe: every frame the
bus carries is idempotent by ply — a client applying `game.move.applied` for
a ply it already has changes nothing — so a duplicate costs a wasted write
and a *missing* delivery costs a stale board. See `RedisStreamGatewayBus`.

## What this is not

Not a broker, not a router, and not a second fan-out. It never consults the
connection registry, never builds a plan and never decides who should receive
anything: the publishing node already did all of that, and this delivers to
the connection ids it was handed. A forwarder that resolved routes would be
the "rediscovering routes inside the publisher" A64-016.3 §8 forbids, moved
one process to the right.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from app.gateway.bus import GatewayBus
from app.gateway.metrics import FORWARDED_FRAMES, FORWARDING_FAILURES
from app.gateway.ports import ConnectionClosed, LocalSocketRegistry
from app.gateway.protocol import MalformedFrame, decode
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ForwardingRun:
    """What one pass achieved.

    Returned rather than only logged so a test can assert the split without
    reading a log, and so the periodic task has something to report at a
    level an operator can alert on.
    """

    consumed: int
    """Bus entries read and acknowledged."""

    delivered: int
    """Frames written to sockets this node holds."""

    missing: int
    """Recipients whose socket this node no longer has.

    **Not an error.** A connection that closed between the publishing node
    building its plan and this pass running is the ordinary case, and the
    registry entry it was resolved from lapses on its own TTL. A rate that
    is persistently high means the fleet's registry is stale rather than
    that delivery is broken.
    """


class GatewayForwarder:
    """Drains this node's bus stream onto its own sockets."""

    def __init__(
        self,
        *,
        bus: GatewayBus,
        sockets: LocalSocketRegistry,
        metrics: MetricsRecorder,
        node_id: str,
        batch_size: int,
    ) -> None:
        self._bus = bus
        self._sockets = sockets
        self._metrics = metrics
        self._node_id = node_id
        self._batch_size = batch_size

    async def forward_once(self) -> ForwardingRun:
        """One bounded pass. Never raises.

        A forwarder that propagated would stop the schedule that called it,
        and a node that has silently stopped receiving remote traffic is
        invisible until somebody's opponent appears to have frozen — the
        same argument `ClockAdjudicationService.adjudicate_once` makes, and
        the same posture.
        """
        messages = await self._bus.consume(self._node_id, limit=self._batch_size)
        if not messages:
            return ForwardingRun(consumed=0, delivered=0, missing=0)

        delivered = 0
        missing = 0

        for message in messages:
            written, absent = await self._deliver(message.frame, message.connection_ids)
            delivered += written
            missing += absent

        self._metrics.increment(FORWARDED_FRAMES, by=delivered)
        self._metrics.increment(FORWARDING_FAILURES, by=missing)

        # One line per pass, not per frame (§15): the payload is a move, and
        # a log of every forwarded frame would be a searchable record of
        # every game played across the fleet.
        logger.info(
            "gateway_forwarding_completed",
            extra={"consumed": len(messages), "delivered": delivered, "missing": missing},
        )
        return ForwardingRun(consumed=len(messages), delivered=delivered, missing=missing)

    async def _deliver(self, frame: str, connection_ids: tuple[str, ...]) -> tuple[int, int]:
        """Writes one frame to the named local connections.

        The frame is decoded **once** for the whole recipient list rather
        than per socket: `GatewaySocket.send` takes an envelope, and a
        decode per connection would parse identical JSON as many times as
        the publishing node found tabs on this one.
        """
        # `max_bytes` is the frame's own length, which reads like disabling
        # the guard and is the correct bound here: the limit exists to stop
        # a *client* sending an enormous frame, and this frame was built by
        # a peer node. Holding it to the client limit would refuse a legal
        # server-sent snapshot of a long game.
        try:
            message = decode(frame, max_bytes=len(frame) + 1)
        except MalformedFrame as malformed:
            # A frame this node cannot parse came from a node running a
            # different build. Dropped rather than retried — it will not
            # become parseable — and logged at `ERROR` because a rising rate
            # is a fleet mid-deploy with an incompatible envelope.
            logger.error(
                "gateway_forwarding_frame_malformed",
                extra={"detail": malformed.detail},
            )
            return 0, len(connection_ids)

        delivered = 0
        missing = 0

        for raw_id in connection_ids:
            connection_id = _connection_id_of(raw_id)
            if connection_id is None:
                missing += 1
                continue

            socket = self._sockets.socket_for(connection_id)
            if socket is None:
                missing += 1
                continue

            try:
                await socket.send(message)
            except ConnectionClosed:
                # The ordinary case — a player closed a tab between the
                # publishing node's plan and this write.
                missing += 1
            except Exception as exc:  # noqa: BLE001 — one socket must not stop a pass
                missing += 1
                logger.warning(
                    "gateway_forwarding_send_failed", extra={"error": type(exc).__name__}
                )
            else:
                delivered += 1

        return delivered, missing


def _connection_id_of(raw: str) -> UUID | None:
    """A wire connection id as a `UUID`, or `None` if it is not one.

    `BusMessage` carries strings because it is a wire type, and this is the
    one place they become identifiers again. A value that is not a UUID
    cannot reach here — the publisher builds them from `ConnectionRoute` —
    but `forward_once` promises never to raise, and a promise with one
    unhandled parse in it is not a promise. Counted as missing and logged,
    which is what an operator needs to see a fleet running two builds.
    """
    try:
        return UUID(raw)
    except ValueError:
        logger.error("gateway_forwarding_recipient_malformed")
        return None


__all__ = ["ForwardingRun", "GatewayForwarder"]
