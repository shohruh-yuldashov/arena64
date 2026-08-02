"""Fan-out over `RoutingPlan` — A64-016.3 §8, §9, §10.

A64-016.2 built the plan and left it with no caller, which its own
documentation recorded as deliberate: "the *decision* is built … what is
missing is delivery". This is the delivery, and it is deliberately thin —
the plan already decided everything.

    resolve recipients  ->  RoutingPlan  ->  local: write to sockets
                                         ->  remote: one publish per node

§8 forbids bypassing the plan and forbids rediscovering routes inside the
publisher. Both are structural here: `deliver` takes the recipients, asks
the router once, and the publisher receives a `ForwardingRequest` that
already names the connections — it has no registry to consult and could not
rediscover anything if it wanted to.

## Delivery never fails a move

§9 and §10 both say so, from opposite directions: "failures do not roll back
an already accepted move", and "do not fail the move because one socket
disconnected". By the time this runs the move is committed in `game`, so
there is nothing to roll back and nothing useful to report to the submitter
— their acknowledgement has already been sent.

So every failure below is **counted and logged**, never raised. A socket that
went away between the plan and the write is the ordinary case, not an
exceptional one: a player closing a tab mid-move produces exactly it.

## One publish per node, and why that is the interesting bound

The remote half is grouped by node in `RoutingPlan` itself, so a fan-out to a
player with four tabs on one node is **one** publish carrying four connection
ids. A publisher that iterated routes would send the same frame four times to
the same process — which is the failure `REMOTE_PUBLISHES` exists to make
visible, because it looks identical to a working system until the fleet
grows.

## What is not here

No retry, no ordering guarantee, no acknowledgement of remote delivery, no
transport. The publisher is a port with a logging implementation behind it
until A64-016.4 gives it a bus (§9 — "do not build a full distributed gateway
broker in this task").
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from app.gateway.metrics import (
    LOCAL_DELIVERIES,
    REMOTE_PUBLISH_FAILURES,
    REMOTE_PUBLISHES,
)
from app.gateway.ports import (
    ConnectionClosed,
    ConnectionRoute,
    ConnectionRouter,
    ForwardingRequest,
    GatewaySocket,
    LocalSocketRegistry,
    RemoteNodePublisher,
)
from app.gateway.protocol import GatewayMessage
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """What a fan-out actually achieved.

    Returned rather than logged-and-forgotten so the caller can record one
    line per move instead of one per recipient (§15 forbids per-frame
    logging), and so a test can assert the split without reading a log.
    """

    local: int
    """Frames written to sockets this node holds."""

    remote_nodes: int
    """Nodes a forwarding request was published to. **Nodes, not
    connections** — see this module's docstring."""

    failures: int
    """Recipients that could not be reached: a socket that had gone away,
    or a node whose publish was refused."""


class RoomBroadcaster:
    """Sends one frame to every live connection of a set of players."""

    def __init__(
        self,
        *,
        router: ConnectionRouter,
        sockets: LocalSocketRegistry,
        publisher: RemoteNodePublisher,
        metrics: MetricsRecorder,
    ) -> None:
        self._router = router
        self._sockets = sockets
        self._publisher = publisher
        self._metrics = metrics

    async def deliver(
        self, message: GatewayMessage, *, recipients: Sequence[UUID]
    ) -> DeliveryReport:
        """Fans `message` out. Never raises.

        The frame is encoded **once**, before the split, for two reasons:
        the remote half needs a string anyway (`ForwardingRequest.frame`),
        and encoding per recipient would make a fan-out to a player with
        four tabs do four `json.dumps` of identical bytes.
        """
        plan = await self._router.plan_for(recipients)
        frame = message.to_json()

        delivered, lost = await self._deliver_locally(plan.local, message)
        published, refused = await self._publish_remotely(plan.remote, frame)

        return DeliveryReport(local=delivered, remote_nodes=published, failures=lost + refused)

    async def _deliver_locally(
        self, routes: Sequence[ConnectionRoute], message: GatewayMessage
    ) -> tuple[int, int]:
        """Writes to the sockets this process holds.

        A route whose socket is absent is counted as lost and **not**
        retried or reported upward: the connection closed between the plan
        being built and the frame being written, which §10 requires to be
        tolerated. The registry entry lapses on its own TTL, and the room
        member is removed by that connection's own cleanup.
        """
        delivered = 0
        lost = 0

        for route in routes:
            socket = self._sockets.socket_for(route.connection_id)
            if socket is None:
                lost += 1
                continue

            try:
                await socket.send(message)
            except ConnectionClosed:
                # The ordinary case, not an error — a player closed a tab.
                lost += 1
            except Exception as exc:  # noqa: BLE001 — one socket must not stop a fan-out
                lost += 1
                logger.warning("gateway_local_delivery_failed", extra={"error": type(exc).__name__})
            else:
                delivered += 1

        self._metrics.increment(LOCAL_DELIVERIES, by=delivered)
        return delivered, lost

    async def _publish_remotely(
        self, remote: Mapping[str, Sequence[ConnectionRoute]], frame: str
    ) -> tuple[int, int]:
        """Hands one forwarding request to each remote node.

        Iterates the plan's **grouping**, which is what makes "one publish
        per node" structural rather than a rule this code has to remember —
        there is no per-connection loop here to get wrong.
        """
        published = 0
        refused = 0

        for node_id, routes in remote.items():
            request = ForwardingRequest(
                node_id=node_id,
                connection_ids=tuple(route.connection_id for route in routes),
                frame=frame,
            )
            if await self._publisher.publish(request):
                published += 1
            else:
                refused += 1

        self._metrics.increment(REMOTE_PUBLISHES, by=published)
        self._metrics.increment(REMOTE_PUBLISH_FAILURES, by=refused)
        return published, refused


class LoggingRemoteNodePublisher:
    """The `RemoteNodePublisher` that ships until there is a bus.

    **A seam, not a stub**, and the distinction is the same one
    `LoggingPendingMatchSink` records: everything upstream is real — the
    routing plan is computed from the live registry, the grouping is
    correct, the frame is the exact bytes a socket would receive. What is
    missing is only the transport.

    It reports success, which is the honest answer for "the request was
    handed to the thing that carries it" when that thing is a log. A
    deployment running more than one gateway node today would therefore
    have silently undelivered frames — which is why `REMOTE_PUBLISHES`
    exists and why single-node is the only supported topology until
    A64-016.4. Stated in `docs/01-architecture/websocket.md` §16 rather
    than left to be discovered.
    """

    async def publish(self, request: ForwardingRequest) -> bool:
        # No frame in the log. The payload is a move, and §15 forbids
        # logging board state — a log of every frame would be a searchable
        # record of every game.
        logger.info(
            "gateway_remote_publish",
            extra={"connections": len(request.connection_ids)},
        )
        return True


class InMemoryLocalSockets:
    """`LocalSocketRegistry` as a dict.

    The whole implementation, and it is not a placeholder: a socket is a
    file descriptor this process owns, so the map that finds one is
    genuinely process-local state. See the port on why that is not the
    process-local *correctness* mechanism §6 forbids.

    One instance per process, wired at the composition root — a per-request
    instance would be a registry that never contains anything.
    """

    def __init__(self) -> None:
        self._sockets: dict[UUID, GatewaySocket] = {}

    def attach(self, connection_id: UUID, socket: GatewaySocket) -> None:
        self._sockets[connection_id] = socket

    def detach(self, connection_id: UUID) -> None:
        # `pop` with a default: cleanup runs on every path including one
        # already handling a failure, and a `KeyError` there would replace
        # the original error with a less useful one.
        self._sockets.pop(connection_id, None)

    def socket_for(self, connection_id: UUID) -> GatewaySocket | None:
        return self._sockets.get(connection_id)

    def __len__(self) -> int:
        """How many sockets this process holds. For an operator and for a
        test; nothing branches on it."""
        return len(self._sockets)


__all__ = [
    "DeliveryReport",
    "InMemoryLocalSockets",
    "LoggingRemoteNodePublisher",
    "RoomBroadcaster",
]
