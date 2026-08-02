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
from app.gateway.protocol import (
    PROTOCOL_VERSION,
    Channel,
    GatewayErrorCode,
    MessageType,
)
from app.gateway.room_service import GameRoomService
from tests.fakes.gateway import (
    FakeConnectionRegistry,
    FakeGatewaySocket,
    FakeRoomMemberStore,
    FakeTicketRedeemer,
    RecordingPresence,
    StubMatchRosters,
)
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

#: A64-016.2. Every connection this "process" opens is registered under it,
#: which is what makes the routing seam's local/remote split meaningful.
NODE_ID = "test-node"

POLICY = GatewayPolicy(
    connection_ttl_seconds=90,
    # Generous: no test here is about the deadline itself, and a short one
    # would make every test a race against its own scripted frames.
    heartbeat_timeout_seconds=30.0,
    max_frame_bytes=8 * 1024,
    node_id=NODE_ID,
)

VALID_TICKET = "a-ticket-a-client-was-handed"


def _frame(message_type: str, **fields: object) -> str:
    """One client frame, encoded the way a browser would send it.

    `channel` is optional at the call site as it is on the wire — an
    A64-016.1 client sends none and every frame it sends is a system frame,
    which is what makes the field a backwards-compatible addition.
    """
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


def _rooms(
    rosters: StubMatchRosters | None = None, members: FakeRoomMemberStore | None = None
) -> GameRoomService:
    """The real room service over in-memory storage.

    Real, not stubbed: the membership rule is what §7 is about, and a test
    that substituted it would assert that the lifecycle calls something
    rather than that only participants get in.
    """
    return GameRoomService(
        rosters=rosters if rosters is not None else StubMatchRosters(),
        members=members if members is not None else FakeRoomMemberStore(),
        metrics=RecordingMetrics(),
        clock=MovableClock(NOW),
        room_ttl_seconds=3600,
    )


def _service(
    tickets: FakeTicketRedeemer,
    registry: FakeConnectionRegistry,
    presence: RecordingPresence,
    rooms: GameRoomService | None = None,
) -> GatewayConnectionService:
    return GatewayConnectionService(
        tickets=tickets,
        registry=registry,
        rooms=rooms if rooms is not None else _rooms(),
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
            rooms=_rooms(),
            presence=presence,
            metrics=RecordingMetrics(),
            clock=MovableClock(NOW),
            policy=GatewayPolicy(
                connection_ttl_seconds=90,
                heartbeat_timeout_seconds=0.001,
                max_frame_bytes=8 * 1024,
                node_id=NODE_ID,
            ),
        )

        await service.run(socket, ticket=VALID_TICKET)

        assert socket.closed_with is not None
        assert socket.closed_with[1] == CloseReason.HEARTBEAT_TIMEOUT.value
        assert presence.states_for(player_id) == [True, False]
        assert await registry.active_count(player_id) == 0


class TestGameRooms:
    """A64-016.2 §7 and §8 — who may attach a socket to a match.

    The room service runs **for real** here, over in-memory storage: the
    membership rule is the whole subject, and a stub in its place would
    assert that the lifecycle calls something rather than that only
    participants get in.
    """

    @pytest.mark.asyncio
    async def test_a_match_participant_joins_and_the_room_reports_who_is_there(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§12.5, and the channel half of §12.8 with it.

        Three things at once, because none of them is worth a connection of
        its own: the join is admitted for a participant, the confirmation
        comes back on the **`game` channel** rather than `system` (which is
        what makes AD-11's multiplexing observable), and `both_connected` is
        `False` while the opponent has not arrived — the state a client
        renders as "waiting for your opponent".
        """
        player_id, opponent_id, match_id = generate_uuid7(), generate_uuid7(), generate_uuid7()
        rosters = StubMatchRosters()
        rosters.add(match_id, light=player_id, dark=opponent_id)

        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [_frame("room.join", channel="game", payload={"match_id": str(match_id)})]
        )

        await _service(tickets, registry, presence, _rooms(rosters)).run(
            socket, ticket=VALID_TICKET
        )

        joined = next(m for m in socket.sent if m.type is MessageType.ROOM_JOINED)
        assert joined.channel is Channel.GAME
        assert joined.payload["both_connected"] is False
        assert set(joined.payload["participants"]) == {str(player_id), str(opponent_id)}

    @pytest.mark.asyncio
    async def test_a_player_who_is_not_in_the_match_is_refused(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§12.6, and the disclosure rule with it.

        A non-participant and an unknown match get the **same** code, which
        is deliberate: a client that could tell them apart could enumerate
        live match identifiers by sending join frames — the argument
        `MatchAcceptanceUseCase.accept` makes for collapsing both into
        `MatchNotFound`.

        Nothing is attached in either case, asserted against the store
        rather than the reply, because a refusal that still wrote a member
        would leave the room reporting a participant who was never admitted.
        """
        outsider, match_id = generate_uuid7(), generate_uuid7()
        rosters, members = StubMatchRosters(), FakeRoomMemberStore()
        rosters.add(match_id, light=generate_uuid7(), dark=generate_uuid7())

        tickets.add(VALID_TICKET, outsider)
        socket = FakeGatewaySocket(
            [
                _frame("room.join", channel="game", payload={"match_id": str(match_id)}),
                _frame("room.join", channel="game", payload={"match_id": str(generate_uuid7())}),
            ]
        )

        await _service(tickets, registry, presence, _rooms(rosters, members)).run(
            socket, ticket=VALID_TICKET
        )

        refusals = [m for m in socket.sent if m.type is MessageType.ERROR]
        assert len(refusals) == 2
        assert all(
            m.payload == {"code": GatewayErrorCode.NOT_A_PARTICIPANT.value} for m in refusals
        )
        assert MessageType.ROOM_JOINED not in socket.types()
        assert members.rooms.get(match_id, []) == []

    @pytest.mark.asyncio
    async def test_one_connection_leaving_keeps_the_players_other_connection_in_the_room(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§12.7, and §8's "disconnecting one connection must not remove the
        player's other connections".

        Two real lifecycles for one player, concurrently — the second tab
        joins and then closes while the first is still open. The first must
        still be in the room afterwards, which is only true because a member
        is the `(player, connection)` pair: a store keyed on the player
        alone would have removed the player entirely on the second tab's
        disconnect, and the opponent would see them leave.

        Asserted against the store, because the room's own view is what
        would be wrong if this broke.
        """
        player_id, match_id = generate_uuid7(), generate_uuid7()
        rosters, members = StubMatchRosters(), FakeRoomMemberStore()
        rosters.add(match_id, light=player_id, dark=generate_uuid7())
        rooms = _rooms(rosters, members)
        service = _service(tickets, registry, presence, rooms)

        join = _frame("room.join", channel="game", payload={"match_id": str(match_id)})
        tickets.add("first-tab", player_id)
        tickets.add("second-tab", player_id)
        first = FakeGatewaySocket([join], holds_open=True)
        second = FakeGatewaySocket([join])

        held = asyncio.create_task(service.run(first, ticket="first-tab"))
        await asyncio.sleep(0)
        await service.run(second, ticket="second-tab")

        assert len(members.rooms[match_id]) == 1
        assert (await rooms.room_of(match_id)).connections_of(player_id) != ()

        first.hang_up()
        await held

        assert members.rooms[match_id] == []

    @pytest.mark.asyncio
    async def test_a_leave_is_idempotent_and_answers_on_the_game_channel(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§8's "repeated leave is idempotent", and the rest of §12.8.

        Three leaves for one join. A client that retries after a dropped
        response, or whose disconnect cleanup races its own `room.leave`,
        must get the outcome it asked for rather than an error — it asked to
        be out and it is out.

        Every answer comes back on the `game` channel, including the ones
        for a room the connection was never in: a client multiplexing three
        streams attributes a reply by its channel, and an idempotent
        acknowledgement arriving on `system` would be unattributable.
        """
        player_id, match_id = generate_uuid7(), generate_uuid7()
        rosters = StubMatchRosters()
        rosters.add(match_id, light=player_id, dark=generate_uuid7())

        leave = _frame("room.leave", channel="game", payload={"match_id": str(match_id)})
        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [
                _frame("room.join", channel="game", payload={"match_id": str(match_id)}),
                leave,
                leave,
                leave,
            ]
        )

        await _service(tickets, registry, presence, _rooms(rosters)).run(
            socket, ticket=VALID_TICKET
        )

        left = [m for m in socket.sent if m.type is MessageType.ROOM_LEFT]
        assert len(left) == 3
        assert all(m.channel is Channel.GAME for m in left)
        assert MessageType.ERROR not in socket.types()
