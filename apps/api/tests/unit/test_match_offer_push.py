"""Pushing a match offer onto a socket — A64-020.5D §23.

The transport half of A64-015.5's seam, now that it has one. What is
asserted here is the *delivery*: who receives an offer, what crosses the
wire, and what happens when nobody is connected — not whether the offer was
correctly resolved, which is `test_pending_match_notifier.py`'s.

The **real** `GatewayPendingMatchSink` over the **real** `RoomBroadcaster`,
`FleetConnectionRouter` and `InMemoryLocalSockets`. What is substituted is
Redis (the registry and the bus) and the socket itself, so the routing
decision — this node or another — is exercised rather than assumed.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.gateway.delivery import InMemoryLocalSockets, RoomBroadcaster
from app.gateway.matchmaking_offers import GatewayPendingMatchSink
from app.gateway.metrics import MATCH_OFFER_PUSHES, MatchOfferOutcome
from app.gateway.protocol import MessageType
from app.gateway.routing import FleetConnectionRouter
from app.modules.game.public import (
    MatchRecordStatus,
    MatchTimeControl,
    PlayerSide,
    ProductVariant,
)
from app.modules.matchmaking.public import OpponentPreview, PendingMatchOffer
from tests.fakes.gateway import (
    FakeConnectionRegistry,
    FakeGatewaySocket,
    RecordingRemotePublisher,
)
from tests.fakes.metrics import RecordingMetrics

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
NODE_ID = "node-a"


def _offer(recipient: UUID, *, opponent: OpponentPreview | None = None) -> PendingMatchOffer:
    return PendingMatchOffer(
        recipient_id=recipient,
        match_id=uuid4(),
        status=MatchRecordStatus.PENDING_ACCEPTANCE,
        your_side=PlayerSide.LIGHT,
        opponent=opponent,
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        time_control=MatchTimeControl(initial_ms=60_000, increment_ms=0),
        speed_class="bullet",
        acceptance_deadline=NOW + timedelta(seconds=30),
        you_accepted=False,
        opponent_accepted=False,
        created_at=NOW,
    )


def _sink(
    registry: FakeConnectionRegistry,
    sockets: InMemoryLocalSockets,
    publisher: RecordingRemotePublisher,
    metrics: RecordingMetrics,
) -> GatewayPendingMatchSink:
    """The real sink over the real fan-out."""
    return GatewayPendingMatchSink(
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


class TestMatchOfferDelivery:
    @pytest.mark.asyncio
    async def test_an_offer_reaches_only_its_recipient_and_carries_no_internals(self) -> None:
        """§2 and §21 — addressed delivery, and what may cross.

        The **only-its-recipient** half is the security assertion. Each
        participant's offer names *their* side and *their* view of the
        opponent, so a fan-out to both would hand each player the other's
        card — and `deliver` takes `recipients=[offer.recipient_id]` for
        exactly that reason. Asserted by giving the opponent a live socket
        on this node and proving it stays empty.

        The payload assertion is the other half of §2: every field is one
        the recipient could already read from
        `GET /matchmaking/matches/pending`, and nothing else. Checked
        against the serialised frame rather than the object, because that is
        what actually leaves.
        """
        recipient, opponent_id = uuid4(), uuid4()
        registry = FakeConnectionRegistry()
        sockets = InMemoryLocalSockets()
        metrics = RecordingMetrics()

        their_connection, opponent_connection = uuid4(), uuid4()
        their_socket, opponent_socket = FakeGatewaySocket(), FakeGatewaySocket()
        sockets.attach(their_connection, their_socket)
        sockets.attach(opponent_connection, opponent_socket)
        await registry.register(recipient, their_connection, node_id=NODE_ID, ttl_seconds=90)
        await registry.register(opponent_id, opponent_connection, node_id=NODE_ID, ttl_seconds=90)

        offer = _offer(
            recipient,
            opponent=OpponentPreview(player_id=opponent_id, username="rival", display_name="Rival"),
        )
        await _sink(registry, sockets, RecordingRemotePublisher(), metrics).deliver([offer])

        assert their_socket.types() == [MessageType.MATCH_OFFERED]
        # The opponent is connected on this very node and receives nothing.
        assert opponent_socket.sent == []

        payload = their_socket.sent[0].payload
        assert payload["match_id"] == str(offer.match_id)
        assert payload["your_side"] == "light"
        assert payload["time_control"] == {"initial_ms": 60_000, "increment_ms": 0}
        assert payload["opponent"] == {
            "player_id": str(opponent_id),
            "username": "rival",
            "display_name": "Rival",
        }
        # Nothing about the queue, the pairing, or storage — §2's list of
        # what must not cross, asserted against the encoded frame.
        encoded = their_socket.sent[0].to_json()
        for forbidden in ("ticket", "pairing", "token", "email", "redis", "queue_"):
            assert forbidden not in encoded.lower()

        assert metrics.counts(MATCH_OFFER_PUSHES) == {MatchOfferOutcome.LOCAL.value: 1.0}

    @pytest.mark.asyncio
    async def test_a_player_on_another_node_is_reached_through_the_bus(self) -> None:
        """§4 — an offer created on node A reaches a player on node B.

        The routing decision is the real `FleetConnectionRouter`'s, so this
        exercises the same partition the move fan-out uses: the registry
        says the connection lives on `node-b`, this process is `node-a`, and
        the frame goes to the publisher rather than to a local socket.

        Delivery *there* is `GatewayForwarder`'s and is already covered by
        `test_gateway_connection.py`; what is asserted here is that the
        offer path uses the fleet-wide route at all rather than assuming
        one process.
        """
        recipient = uuid4()
        registry = FakeConnectionRegistry()
        sockets = InMemoryLocalSockets()
        publisher = RecordingRemotePublisher()
        metrics = RecordingMetrics()

        elsewhere = uuid4()
        await registry.register(recipient, elsewhere, node_id="node-b", ttl_seconds=90)

        offer = _offer(recipient)
        await _sink(registry, sockets, publisher, metrics).deliver([offer])

        assert len(publisher.published) == 1
        request = publisher.published[0]
        assert request.node_id == "node-b"
        assert request.connection_ids == (elsewhere,)
        assert str(offer.match_id) in request.frame
        assert metrics.counts(MATCH_OFFER_PUSHES) == {MatchOfferOutcome.REMOTE.value: 1.0}

    @pytest.mark.asyncio
    async def test_nobody_connected_is_counted_and_never_raised(self) -> None:
        """§3 and §20 — delivery is an optimisation.

        A player who queued and closed the tab has no connection, and that
        is the **ordinary** state rather than a failure: the durable read is
        what tells them when they come back. So the sink counts it and
        returns.

        The assertion that matters is that it does not raise. `deliver` runs
        inside the relay consumer, and an exception here would fail the
        whole outbox entry — a match pairing retried because a socket was
        absent, which §20 forbids.
        """
        registry = FakeConnectionRegistry()
        metrics = RecordingMetrics()

        # Two offers in one batch, one of which has nobody listening: the
        # batch must complete rather than stopping at the first miss.
        connected, absent = uuid4(), uuid4()
        socket = FakeGatewaySocket()
        sockets = InMemoryLocalSockets()
        connection = uuid4()
        sockets.attach(connection, socket)
        await registry.register(connected, connection, node_id=NODE_ID, ttl_seconds=90)

        await _sink(registry, sockets, RecordingRemotePublisher(), metrics).deliver(
            [_offer(absent), _offer(connected)]
        )

        assert socket.types() == [MessageType.MATCH_OFFERED]
        assert metrics.counts(MATCH_OFFER_PUSHES) == {
            MatchOfferOutcome.NO_CONNECTION.value: 1.0,
            MatchOfferOutcome.LOCAL.value: 1.0,
        }
