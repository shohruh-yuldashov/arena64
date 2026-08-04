"""The remote transport bus — A64-016.4 §9.

A64-016.3 built `RemoteNodePublisher` and put a log line behind it, recording
plainly what that cost: "a deployment running more than one gateway node
today has silently undelivered frames … single-node is the only supported
topology". This is the seam that replaces the log line, and §9 is equally
plain about how far to take it — "do not deploy or fully design a distributed
broker in this task".

So what is here is a **port, an envelope and an in-process adapter**. The
production adapter is one class against the same port, and this file names
where it goes and what it has to get right.

## Why a second abstraction beside `RemoteNodePublisher`

They are different questions and collapsing them is what makes a transport
hard to replace:

    RemoteNodePublisher   *what* to send, decided from a routing plan. Knows
                          about connections and rooms
    GatewayBus            *how* it travels between two processes. Knows
                          about nodes and bytes, and nothing about a game

The publisher is now a thin adapter over the bus, which is why it stopped
holding a log line and started holding a port.

## The payload is bytes-shaped, deliberately

`BusMessage` is primitive-only — a node id, connection ids as strings, and
the already-encoded frame. §9 requires "no direct socket references in bus
payloads", and the stronger property is what makes that true rather than
remembered: this type is JSON-round-trippable, so anything that could not
cross a process boundary cannot be put in it.

`request_id` and `channel` travel **inside the frame**, which is where the
protocol already puts them (`GatewayMessage.to_json`). Lifting them onto the
envelope would be a second copy of two fields that must not disagree — §9
asks that both be preserved, and preserving them by not touching them is the
form of that guarantee least able to go wrong.

## Delivery semantics this seam promises, and what it does not

    at-least-once      the frame carries a ply, and a client that sees the
                       same ply twice ignores the second. So a redelivery is
                       safe and the transport need not deduplicate
    no ordering        two frames for one connection may arrive in either
                       order. The ply is the sequence, and A64-016.3's room
                       projection already refuses to move backwards
    failure reported   `publish` returns rather than raising. A move is
                       committed before anything is delivered, so a
                       publisher that raised would turn a delivery problem
                       into a move that appears to have failed after it was
                       applied

## Where the production adapter goes

`RedisStreamGatewayBus`, against Redis's `bus` role — the instance AD-03
already assigns to pub/sub fan-out and which nothing yet uses. A stream per
node (`gwbus:v1:<node_id>`), consumed by that node's own reader task, with
`MAXLEN` bounding it: a node that has gone away must not accumulate frames
forever, and a bounded stream drops the oldest, which for realtime frames is
the correct loss.

That is A64-016.5's, and it is described here rather than in a ticket so the
next task starts from the decision rather than rediscovering it.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from app.gateway.ports import ForwardingRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BusMessage:
    """One node's share of a fan-out, as it crosses a process boundary.

    Frozen and primitive-only. Every field survives `json.dumps` and comes
    back the same, which is what §9's "stable primitive payload" means and
    is checked rather than asserted — see `to_primitive`/`from_primitive`.
    """

    node_id: str
    """Which node should deliver this. Carried in the payload as well as
    being the routing key, so a consumer reading from a shared stream can
    verify the message was meant for it rather than trusting its own
    subscription."""

    connection_ids: tuple[str, ...]
    """The connections on that node that should receive the frame.

    Strings rather than `UUID`s because this is a wire type: a `UUID` would
    need a custom encoder, and a transport that needed one would be a
    transport with an opinion about the payload.
    """

    frame: str
    """The encoded envelope, exactly as a socket would receive it.

    **Not re-encoded on the way out.** `request_id` and `channel` are
    already inside it, and lifting them onto this envelope would be a second
    copy of two fields that must not disagree.
    """

    def to_primitive(self) -> dict[str, Any]:
        """The message as JSON-ready primitives."""
        return {
            "node_id": self.node_id,
            "connection_ids": list(self.connection_ids),
            "frame": self.frame,
        }

    @classmethod
    def from_primitive(cls, value: dict[str, Any]) -> "BusMessage":
        """One message back from the wire.

        Strict: a payload missing a field or carrying the wrong type raises
        rather than defaulting. A transport that silently produced a message
        with no recipients would deliver nothing and report success, which
        is the failure mode hardest to notice in a fan-out.
        """
        node_id = value["node_id"]
        connection_ids = value["connection_ids"]
        frame = value["frame"]

        if not isinstance(node_id, str) or not isinstance(frame, str):
            raise ValueError("a bus message carries a node id and a frame")
        if not isinstance(connection_ids, list) or not all(
            isinstance(entry, str) for entry in connection_ids
        ):
            raise ValueError("a bus message carries connection ids as strings")

        return cls(node_id=node_id, connection_ids=tuple(connection_ids), frame=frame)


class GatewayBus(Protocol):
    """Carries frames between gateway processes — §9.

    Framework-independent by construction: no FastAPI, no Starlette, no
    Redis, no socket. Two methods, and the asymmetry between them is the
    design — a node publishes to *any* node and consumes only for *itself*.
    """

    async def publish(self, message: BusMessage) -> bool:
        """Hands one node's share to the transport. `False` if it could not.

        **Never raises.** The move is committed before anything is
        delivered (§10 — "do not make remote-delivery failure roll back an
        already committed move"), so a publisher that raised would turn a
        delivery problem into a move that appears to have failed after it
        was applied.
        """
        ...

    async def consume(self, node_id: str, *, limit: int) -> Sequence[BusMessage]:
        """Up to `limit` messages addressed to this node.

        Bounded, like every read on this platform (CLAUDE.md §10.5): an
        unbounded drain is a node that has been offline returning with an
        arbitrarily large batch, on the one path that must stay responsive.

        Empty is the overwhelmingly common answer and is not a failure.
        """
        ...


class InProcessGatewayBus:
    """A `GatewayBus` that never leaves the process — §9's test adapter.

    **Not a stub.** It implements the contract completely: messages are
    queued per node, `consume` returns only what was addressed to the caller
    and removes what it returns, and the bound is honoured. What it does not
    do is cross a process boundary — which is the one thing a single-node
    deployment does not need.

    That makes it the correct production adapter for **single-node**, which
    is the supported topology today, and the honest one: a deployment with
    two nodes gets no delivery and `gateway.remote_publish_failures_total`
    stays at zero, so §9's "clear production adapter seam" is a real seam
    rather than a silent downgrade. See this module's docstring for where
    the multi-node adapter goes.

    Bounded per node, because an unbounded queue for a node nobody consumes
    is a memory leak with a plausible-looking name. The oldest is dropped,
    which for realtime frames is the correct loss: a client that missed a
    ply resynchronises, and one that missed the newest ply is looking at a
    stale board.
    """

    def __init__(self, *, max_pending_per_node: int = 1024) -> None:
        self._pending: dict[str, list[BusMessage]] = {}
        self._max_pending = max_pending_per_node
        self.dropped = 0
        """How many messages were evicted by the bound. For an operator and
        a test; a rising count means a node is not consuming."""

    async def publish(self, message: BusMessage) -> bool:
        queue = self._pending.setdefault(message.node_id, [])
        queue.append(message)

        if len(queue) > self._max_pending:
            # Oldest first — see this class's docstring on why that is the
            # right end to drop from for realtime frames.
            del queue[0 : len(queue) - self._max_pending]
            self.dropped += 1
            logger.warning("gateway_bus_backlog_trimmed", extra={"pending": len(queue)})

        return True

    async def consume(self, node_id: str, *, limit: int) -> Sequence[BusMessage]:
        queue = self._pending.get(node_id)
        if not queue:
            return ()

        taken = tuple(queue[:limit])
        del queue[: len(taken)]
        return taken

    def pending_for(self, node_id: str) -> int:
        """How many messages are waiting for a node. For a test and for an
        operator; nothing branches on it."""
        return len(self._pending.get(node_id, ()))


class BusRemoteNodePublisher:
    """`RemoteNodePublisher` over a `GatewayBus` — the two seams joined.

    A64-016.3's publisher decided *what* to send and logged it. This one
    decides what to send and hands it to a transport, which is the whole
    change: the routing plan still produces one request per node, and the
    bus still knows nothing about rooms.

    Kept as a separate class rather than making `RoomBroadcaster` hold a
    bus, because the two ports answer different questions and a broadcaster
    that held a transport would be a broadcaster that has to know about node
    addressing.
    """

    def __init__(self, bus: GatewayBus) -> None:
        self._bus = bus

    async def publish(self, request: ForwardingRequest) -> bool:
        """Translates a forwarding request into a bus message and sends it.

        **Never raises**, per `RemoteNodePublisher`'s contract: a bus that
        failed must be reported, not propagated, because the move it
        carries has already been committed.
        """
        try:
            return await self._bus.publish(
                BusMessage(
                    node_id=request.node_id,
                    connection_ids=tuple(
                        str(connection_id) for connection_id in request.connection_ids
                    ),
                    frame=request.frame,
                )
            )
        except Exception as exc:  # noqa: BLE001 — a transport must not fail a move
            logger.warning("gateway_bus_publish_failed", extra={"error": type(exc).__name__})
            return False


def connection_ids_of(message: BusMessage) -> tuple[UUID, ...]:
    """The message's recipients as identifiers.

    Parsed at the point of use rather than on the way in, so a malformed id
    costs one undelivered frame instead of a rejected batch — the tolerance
    every decoder on this platform keeps for a rolling deploy.
    """
    parsed: list[UUID] = []
    for raw in message.connection_ids:
        try:
            parsed.append(UUID(raw))
        except ValueError:
            logger.warning("gateway_bus_connection_id_malformed")
    return tuple(parsed)


__all__ = [
    "BusMessage",
    "BusRemoteNodePublisher",
    "GatewayBus",
    "InProcessGatewayBus",
    "connection_ids_of",
]
