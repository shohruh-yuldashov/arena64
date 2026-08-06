"""Announcing a notification onto a socket — A64-021.2 §1, §3, §7, §10.

The transport half of A64-021.1's seam, now that it has one. What is
asserted here is the *delivery*: who receives an announcement, what crosses
the wire, and what happens when the fan-out fails — not whether the
notification was correctly produced, which is
`tests/contract/test_notifications_api.py`'s.

The **real** `GatewayNotificationSink` over the **real** `RoomBroadcaster`,
`FleetConnectionRouter` and `InMemoryLocalSockets`. What is substituted is
Redis (the registry and the bus) and the socket itself, so the routing
decision — this node or another — is exercised rather than assumed.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.gateway.delivery import InMemoryLocalSockets, RoomBroadcaster
from app.gateway.metrics import NOTIFICATION_PUSHES, NotificationPushOutcome
from app.gateway.notifications import GatewayNotificationSink
from app.gateway.ports import ConnectionRoute
from app.gateway.protocol import GatewayMessage, MessageType
from app.gateway.routing import FleetConnectionRouter
from app.modules.notifications.public import NotificationAnnouncement, NotificationType
from tests.fakes.gateway import (
    FakeConnectionRegistry,
    FakeGatewaySocket,
    RecordingRemotePublisher,
)
from tests.fakes.metrics import RecordingMetrics

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
NODE_ID = "node-a"


def _announcement(recipient: UUID) -> NotificationAnnouncement:
    return NotificationAnnouncement(
        notification_id=uuid4(),
        recipient_id=recipient,
        type=NotificationType.FRIEND_REQUEST_RECEIVED,
        created_at=NOW,
    )


def _sink(
    registry: FakeConnectionRegistry,
    sockets: InMemoryLocalSockets,
    publisher: RecordingRemotePublisher,
    metrics: RecordingMetrics,
) -> GatewayNotificationSink:
    """The real sink over the real fan-out."""
    return GatewayNotificationSink(
        broadcaster=RoomBroadcaster(
            router=FleetConnectionRouter(
                registry=registry, node_id=NODE_ID, metrics=RecordingMetrics()
            ),
            sockets=sockets,
            publisher=publisher,
            metrics=RecordingMetrics(),
        ),
        metrics=metrics,
    )


class TestNotificationDelivery:
    @pytest.mark.asyncio
    async def test_it_reaches_only_its_recipient_and_carries_only_three_fields(self) -> None:
        """§2 and §3 — addressed delivery, and what may cross.

        The **only-its-recipient** half is the security assertion, and it is
        stronger here than for a match offer: a notification has no second
        participant and no audience, so any other socket receiving one would
        be a leak rather than a duplicate. Asserted by giving another player
        a live socket on this very node and proving it stays empty.

        The payload assertion is §2's whole design: `notification_id`, `type`
        and `created_at`, and **nothing else**. Checked as an exact key set
        rather than by looking for forbidden substrings, because "no field
        was added" is the property — a fourth field with an innocuous name
        would pass a substring check and fail this one.
        """
        recipient, bystander = uuid4(), uuid4()
        registry = FakeConnectionRegistry()
        sockets = InMemoryLocalSockets()
        metrics = RecordingMetrics()

        their_connection, bystander_connection = uuid4(), uuid4()
        their_socket, bystander_socket = FakeGatewaySocket(), FakeGatewaySocket()
        sockets.attach(their_connection, their_socket)
        sockets.attach(bystander_connection, bystander_socket)
        await registry.register(recipient, their_connection, node_id=NODE_ID, ttl_seconds=90)
        await registry.register(bystander, bystander_connection, node_id=NODE_ID, ttl_seconds=90)

        announcement = _announcement(recipient)
        await _sink(registry, sockets, RecordingRemotePublisher(), metrics).announce([announcement])

        assert their_socket.types() == [MessageType.NOTIFICATION_CREATED]
        # Connected on this very node, and receives nothing.
        assert bystander_socket.sent == []

        frame: GatewayMessage = their_socket.sent[0]
        assert frame.channel.value == "notifications"
        assert frame.payload == {
            "notification_id": str(announcement.notification_id),
            "type": "friend_request_received",
            "created_at": NOW.isoformat(),
        }

        # §2 and §8: not the recipient, not an actor, not a rendered
        # sentence, not a token. Asserted against the encoded frame, because
        # that is what actually leaves the process.
        encoded = frame.to_json()
        for forbidden in (str(recipient), "username", "display_name", "avatar", "email", "token"):
            assert forbidden not in encoded

        assert metrics.counts(NOTIFICATION_PUSHES) == {NotificationPushOutcome.LOCAL.value: 1.0}

    @pytest.mark.asyncio
    async def test_a_recipient_on_another_node_is_reached_through_the_bus(self) -> None:
        """§7 — a notification written on node A reaches a socket on node B.

        The routing decision is the real `FleetConnectionRouter`'s, so this
        exercises the same partition the move fan-out and the match offer
        already use: the registry says the connection lives on `node-b`,
        this process is `node-a`, and the frame goes to the publisher rather
        than to a local socket.

        Delivery *there* is `GatewayForwarder`'s and is already covered by
        `test_gateway_connection.py`; what is asserted here is that the
        notification path uses the fleet-wide route at all rather than
        assuming one process — which is the whole of §7's "works unchanged".
        """
        recipient = uuid4()
        registry = FakeConnectionRegistry()
        sockets = InMemoryLocalSockets()
        publisher = RecordingRemotePublisher()
        metrics = RecordingMetrics()

        elsewhere = uuid4()
        await registry.register(recipient, elsewhere, node_id="node-b", ttl_seconds=90)

        announcement = _announcement(recipient)
        await _sink(registry, sockets, publisher, metrics).announce([announcement])

        assert len(publisher.published) == 1
        request = publisher.published[0]
        assert request.node_id == "node-b"
        assert request.connection_ids == (elsewhere,)
        assert str(announcement.notification_id) in request.frame
        assert metrics.counts(NOTIFICATION_PUSHES) == {NotificationPushOutcome.REMOTE.value: 1.0}

    @pytest.mark.asyncio
    async def test_it_never_raises_and_the_batch_survives_a_failure(self) -> None:
        """§1 and §6 — the announcer is an accelerator and cannot fail a tick.

        Two failure modes in one test, because both must leave the same
        thing standing: the relay tick that carried the batch.

        **Nobody connected** is the ordinary state of a player who is not
        looking at the app, and is counted rather than raised. **A fan-out
        that raises** is the real failure, and it is counted too — the
        notification it announces is *already committed*, so raising would
        retry an event whose durable work is done and hold up every other
        notification in the batch.

        The last announcement in the batch is delivered, which is the
        assertion that matters: a failure at position one must not stop
        position three.
        """
        registry = FakeConnectionRegistry()
        metrics = RecordingMetrics()
        sockets = InMemoryLocalSockets()

        absent, connected = uuid4(), uuid4()
        socket = FakeGatewaySocket()
        connection = uuid4()
        sockets.attach(connection, socket)
        await registry.register(connected, connection, node_id=NODE_ID, ttl_seconds=90)

        exploding = uuid4()

        class _Exploding(FakeConnectionRegistry):
            """A registry that is unreachable for exactly one player.

            The failure is injected at the registry rather than at the
            socket, because that is where a real one lives: `routes_for` is
            a Redis read, and Redis being briefly gone is the failure this
            sink must survive.
            """

            async def routes_for(self, player_id: UUID) -> Sequence[ConnectionRoute]:
                if player_id == exploding:
                    raise RuntimeError("the registry is unreachable")
                return await super().routes_for(player_id)

        failing = _Exploding()
        await failing.register(connected, connection, node_id=NODE_ID, ttl_seconds=90)

        sink = _sink(failing, sockets, RecordingRemotePublisher(), metrics)
        await sink.announce(
            [_announcement(absent), _announcement(exploding), _announcement(connected)]
        )

        # The batch completed and the reachable recipient was reached.
        assert socket.types() == [MessageType.NOTIFICATION_CREATED]
        assert metrics.counts(NOTIFICATION_PUSHES) == {
            NotificationPushOutcome.NO_CONNECTION.value: 1.0,
            NotificationPushOutcome.FAILED.value: 1.0,
            NotificationPushOutcome.LOCAL.value: 1.0,
        }
