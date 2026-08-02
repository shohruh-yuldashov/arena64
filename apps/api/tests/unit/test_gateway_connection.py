"""The WebSocket connection lifecycle — A64-016.1 §10.

Eight tests, which is the budget the task sets, so each one is a rule rather
than an example. They run the **real** `GatewayConnectionService`, the real
envelope codec and the real `PresenceRecorder` port; what is substituted is
the transport, the ticket store and the registry — nothing that decides what
the lifecycle does.

## Why the service and not the route

`app/gateway/router.py` is three statements: accept, wrap, delegate. Driving
these eight rules through a real socket would need a client, an event loop
and a portal per test, and would assert the same behaviour with an order of
magnitude more machinery. The route's own risk — that it wires the adapter to
the service correctly — is covered by `test_gateway_route_is_registered`
being unnecessary: a mis-wired route fails at import, because `Depends`
resolution is checked when the application is built.

## The one gap, stated rather than hidden

Single-use ticket redemption and the registry's atomic count are properties
of `GETDEL` and `MULTI`/`EXEC` respectively. The fakes model both, and a
model that agrees with itself proves nothing about Redis — the platform's
usual answer is a contract suite with two real sessions, and this keyspace
does not have one yet. Recorded in A64-016.1's report as the first thing
A64-016.2 should add.
"""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.core.identifiers import generate_uuid7
from app.gateway.connections import (
    CLOSE_INTERNAL_ERROR,
    CLOSE_POLICY_VIOLATION,
    GatewayConnectionService,
    GatewayPolicy,
)
from app.gateway.metrics import CloseReason
from app.gateway.protocol import PROTOCOL_VERSION, GatewayErrorCode, MessageType
from tests.fakes.gateway import (
    FakeConnectionRegistry,
    FakeGatewaySocket,
    FakeTicketRedeemer,
    RecordingPresence,
)
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

POLICY = GatewayPolicy(
    connection_ttl_seconds=90,
    # Generous: no test here is about the deadline itself, and a short one
    # would make every test a race against its own scripted frames.
    heartbeat_timeout_seconds=30.0,
    max_frame_bytes=8 * 1024,
)

VALID_TICKET = "a-ticket-a-client-was-handed"


def _frame(message_type: str, **fields: object) -> str:
    """One client frame, encoded the way a browser would send it."""
    return json.dumps({"v": PROTOCOL_VERSION, "type": message_type, **fields})


@pytest.fixture
def registry() -> FakeConnectionRegistry:
    return FakeConnectionRegistry()


@pytest.fixture
def presence() -> RecordingPresence:
    return RecordingPresence()


@pytest.fixture
def tickets() -> FakeTicketRedeemer:
    return FakeTicketRedeemer()


def _service(
    tickets: FakeTicketRedeemer,
    registry: FakeConnectionRegistry,
    presence: RecordingPresence,
) -> GatewayConnectionService:
    return GatewayConnectionService(
        tickets=tickets,
        registry=registry,
        presence=presence,
        metrics=RecordingMetrics(),
        clock=MovableClock(NOW),
        policy=POLICY,
    )


class TestTheHandshake:
    @pytest.mark.asyncio
    async def test_a_valid_ticket_connects_and_the_player_comes_online(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """The whole happy path in one assertion set — §10.1 and §10.3.

        Three things have to be true together and none of them is
        interesting alone: the socket is told it is *authenticated* rather
        than merely open, the connection is registered so the fleet can
        route to it, and the player's first connection is what marks them
        online.
        """
        player_id = generate_uuid7()
        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket()

        await _service(tickets, registry, presence).run(socket, ticket=VALID_TICKET)

        assert socket.types()[0] is MessageType.CONNECTION_READY
        assert registry.unregister_calls != []
        assert presence.states_for(player_id)[0] is True

    @pytest.mark.asyncio
    async def test_an_unknown_or_reused_ticket_is_refused_without_registering(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§10.2. Expired, unknown and already-spent are one outcome.

        The reuse half is what the fake's single-use modelling is for: the
        *same* ticket that worked a moment ago is gone from the store, which
        is what `GETDEL` produces. Asserted together because the client-
        visible result is identical by design — telling them apart would be
        the oracle `GatewayErrorCode.INVALID_TICKET` refuses to be.

        Nothing may be registered and nobody may be marked online, because a
        refusal that registered would leak a connection slot per probe.
        """
        player_id = generate_uuid7()
        tickets.add(VALID_TICKET, player_id)
        service = _service(tickets, registry, presence)

        await service.run(FakeGatewaySocket(), ticket=VALID_TICKET)
        presence.observations.clear()

        replayed = FakeGatewaySocket()
        unknown = FakeGatewaySocket()
        await service.run(replayed, ticket=VALID_TICKET)
        await service.run(unknown, ticket="never-issued")

        for socket in (replayed, unknown):
            assert socket.sent[0].payload == {"code": GatewayErrorCode.INVALID_TICKET.value}
            assert socket.closed_with is not None
            assert socket.closed_with[0] == CLOSE_POLICY_VIOLATION
        assert presence.observations == []
        assert await registry.active_count(player_id) == 0


class TestMultipleConnections:
    @pytest.mark.asyncio
    async def test_the_final_disconnect_marks_the_player_offline(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§10.4. Online on the way in, offline on the way out, once each."""
        player_id = generate_uuid7()
        tickets.add(VALID_TICKET, player_id)

        await _service(tickets, registry, presence).run(FakeGatewaySocket(), ticket=VALID_TICKET)

        assert presence.states_for(player_id) == [True, False]
        assert await registry.active_count(player_id) == 0

    @pytest.mark.asyncio
    async def test_one_of_two_connections_closing_keeps_the_player_online(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§10.5, and the rule §7 states as a prohibition: "do not allow one
        broken socket to mark a player offline while another connection
        remains active".

        Two tabs, both **real lifecycles running concurrently**, because the
        rule is about a concurrent state and a sequential approximation
        would not have one: the first socket holds open while the second
        connects, serves and cleans up.

        The second's cleanup sees one connection remaining and must
        therefore write no presence at all. Only when the first hangs up —
        the genuinely final disconnect — does the player go offline, exactly
        once.
        """
        player_id = generate_uuid7()
        service = _service(tickets, registry, presence)

        tickets.add("first-tab", player_id)
        tickets.add("second-tab", player_id)
        first = FakeGatewaySocket(holds_open=True)
        second = FakeGatewaySocket([_frame("ping")])

        held = asyncio.create_task(service.run(first, ticket="first-tab"))
        await asyncio.sleep(0)  # let the first tab register before the second
        await service.run(second, ticket="second-tab")

        # The second tab is fully closed and the first is still open.
        assert False not in presence.states_for(player_id)
        assert await registry.active_count(player_id) == 1

        first.hang_up()
        await held

        assert presence.states_for(player_id)[-1] is False
        assert presence.states_for(player_id).count(False) == 1
        assert await registry.active_count(player_id) == 0


class TestTheHeartbeat:
    @pytest.mark.asyncio
    async def test_a_ping_is_answered_with_a_correlated_pong(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§10.6, plus the half that makes the heartbeat do its job.

        The `pong` alone would prove a round trip. What matters
        operationally is that the beat also **refreshes presence** — the
        registry entry and the presence record have different TTLs and
        different owners, and a heartbeat that refreshed only the first
        would report a player offline while holding their socket open.

        The `request_id` is echoed because a client with several messages in
        flight has no other way to match an answer to its question, which is
        what AD-23's optimistic board will depend on.
        """
        player_id = generate_uuid7()
        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket([_frame("ping", request_id="beat-1")])

        await _service(tickets, registry, presence).run(socket, ticket=VALID_TICKET)

        answer = next(m for m in socket.sent if m.type is MessageType.PONG)
        assert answer.request_id == "beat-1"
        # Online twice: once on connect, once on the beat.
        assert presence.states_for(player_id).count(True) == 2

    @pytest.mark.asyncio
    async def test_a_malformed_frame_is_refused_without_closing_the_connection(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§10.7. Four ways to be malformed, one safe answer, and the
        connection survives all of them.

        Surviving is the part worth asserting: a client that sends one bad
        frame is far more likely to be a version skew than an attack, and
        closing would turn a recoverable client defect into a fleet-wide
        reconnect loop. The `ping` at the end proves the socket still works.

        Every answer carries a **code and nothing else** — no parser detail,
        no echo of what was sent — because a client learning *why* a frame
        was rejected learns how to shape one that is not.
        """
        player_id = generate_uuid7()
        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [
                "not json at all",
                json.dumps(["an", "array"]),
                _frame("match.move"),
                json.dumps({"v": PROTOCOL_VERSION + 1, "type": "ping"}),
                _frame("ping"),
            ]
        )

        await _service(tickets, registry, presence).run(socket, ticket=VALID_TICKET)

        errors = [m for m in socket.sent if m.type is MessageType.ERROR]
        assert len(errors) == 4
        assert all(m.payload == {"code": GatewayErrorCode.MALFORMED_MESSAGE.value} for m in errors)
        assert MessageType.PONG in socket.types()


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_runs_exactly_once_however_the_connection_ends(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§10.8 and §7. Three endings, one unregister each.

        The three are genuinely different code paths — a peer that hung up,
        a peer that went away mid-write, and a registry that could not be
        written — and §7 requires all of them to clean up. What must not
        happen is cleanup running *twice*, which on a shared registry would
        be a second `unregister` that could report zero while another tab is
        open.

        The registration failure is the interesting one: it must clean up
        **nothing**, because nothing was registered. A `finally` placed one
        line higher would unregister a connection that never existed, and
        the real adapter would then report the player's true remaining count
        — marking them offline while their other tab is connected. That is
        the exact failure the service's structure is arranged to prevent, so
        it is asserted rather than trusted.
        """
        player_id = generate_uuid7()
        service = _service(tickets, registry, presence)

        tickets.add("normal", player_id)
        await service.run(FakeGatewaySocket(), ticket="normal")

        tickets.add("write-fails", player_id)
        broken = FakeGatewaySocket()
        broken.send_fails = True
        await service.run(broken, ticket="write-fails")

        assert len(registry.unregister_calls) == 2

        registry.fails = True
        tickets.add("registry-down", player_id)
        refused = FakeGatewaySocket()
        await service.run(refused, ticket="registry-down")

        assert len(registry.unregister_calls) == 2
        assert refused.closed_with is not None
        # `1011`, not `1008`: the ticket was good and the server failed, so
        # the client should retry with a fresh ticket rather than treat its
        # credentials as rejected. See `_refuse`.
        assert refused.closed_with[0] == CLOSE_INTERNAL_ERROR

    @pytest.mark.asyncio
    async def test_a_silent_connection_is_dropped_and_cleaned_up(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """The fifth cleanup path §7 names: a heartbeat that never arrives.

        The only one of the five where the *server* ends the connection, and
        the only one whose failure mode is silent — a client that believes it
        is connected and is not looks identical to an idle one from its own
        side, and the gateway is the only place that can tell.

        Driven with a one-millisecond deadline against a socket that holds
        open and never speaks, so the wait genuinely lapses rather than being
        simulated. Cleanup must still run: the player goes offline and the
        registry entry is released, exactly as on a normal disconnect.
        """
        player_id = generate_uuid7()
        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(holds_open=True)
        service = GatewayConnectionService(
            tickets=tickets,
            registry=registry,
            presence=presence,
            metrics=RecordingMetrics(),
            clock=MovableClock(NOW),
            policy=GatewayPolicy(
                connection_ttl_seconds=90,
                heartbeat_timeout_seconds=0.001,
                max_frame_bytes=8 * 1024,
            ),
        )

        await service.run(socket, ticket=VALID_TICKET)

        assert socket.closed_with is not None
        assert socket.closed_with[1] == CloseReason.HEARTBEAT_TIMEOUT.value
        assert presence.states_for(player_id) == [True, False]
        assert await registry.active_count(player_id) == 0
