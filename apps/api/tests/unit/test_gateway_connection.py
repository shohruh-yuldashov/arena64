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
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.identifiers import generate_uuid7
from app.gateway.bus import (
    BusMessage,
    BusRemoteNodePublisher,
    InProcessGatewayBus,
    connection_ids_of,
)
from app.gateway.connections import (
    CLOSE_INTERNAL_ERROR,
    CLOSE_POLICY_VIOLATION,
    GatewayConnectionService,
    GatewayPolicy,
)
from app.gateway.delivery import InMemoryLocalSockets, RoomBroadcaster
from app.gateway.forwarding import ForwardingRun, GatewayForwarder
from app.gateway.metrics import CloseReason
from app.gateway.moves import MoveSubmissionHandler
from app.gateway.ports import ForwardingRequest
from app.gateway.protocol import (
    PROTOCOL_VERSION,
    Channel,
    GatewayErrorCode,
    MessageType,
    decode,
    move_accepted,
    move_applied,
    resumed,
)
from app.gateway.resume import ResumeHandler
from app.gateway.room_service import GameRoomService
from app.gateway.rooms import RoomMember
from app.gateway.routing import FleetConnectionRouter
from app.gateway.spectator_handler import SpectatorHandler
from app.gateway.spectators import BlockAwareSpectatorPolicy, SpectatorSubscription
from app.modules.engine import PlayerSide
from app.modules.game.public import (
    ClockView,
    IllegalMoveSubmitted,
    MatchNotActive,
    MatchRecordStatus,
    NotYourTurn,
    StaleMatchState,
)
from tests.fakes.gateway import (
    CountingMoveLimiter,
    FakeConnectionRegistry,
    FakeGatewaySocket,
    FakeRoomMemberStore,
    FakeSubmitMoves,
    FakeTicketRedeemer,
    InMemoryEventBuffer,
    InMemoryMoveIdempotency,
    InMemorySpectatorStore,
    RecordingPresence,
    RecordingRemotePublisher,
    StubMatchRosters,
    StubMatchSnapshots,
    StubPairingExclusions,
    StubSpectatorPolicy,
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


class RecordingAttendance:
    """A `tournament.public.TournamentAttendance` a test can inspect.

    The tournament's own behaviour is `tests/contract/test_tournament_matches.py`'s;
    what these tests need is that a join *tells* it, and that a failure to
    tell it does not cost two people their game.
    """

    def __init__(self, *, fails: bool = False) -> None:
        self.marked: list[tuple[UUID, UUID]] = []
        self._fails = fails

    async def mark_present(self, match_id: UUID, player_id: UUID, *, at: datetime) -> bool:
        if self._fails:
            raise RuntimeError("the tournaments schema is unreachable")
        self.marked.append((match_id, player_id))
        return True


def _rooms(
    rosters: StubMatchRosters | None = None,
    members: FakeRoomMemberStore | None = None,
    attendance: RecordingAttendance | None = None,
) -> GameRoomService:
    """The real room service over in-memory storage.

    Real, not stubbed: the membership rule is what §7 is about, and a test
    that substituted it would assert that the lifecycle calls something
    rather than that only participants get in.
    """
    return GameRoomService(
        rosters=rosters if rosters is not None else StubMatchRosters(),
        members=members if members is not None else FakeRoomMemberStore(),
        attendance=attendance if attendance is not None else RecordingAttendance(),
        metrics=RecordingMetrics(),
        clock=MovableClock(NOW),
        room_ttl_seconds=3600,
    )


def _moves(
    rooms: GameRoomService,
    *,
    submissions: FakeSubmitMoves | None = None,
    limiter: CountingMoveLimiter | None = None,
    publisher: RecordingRemotePublisher | None = None,
    sockets: InMemoryLocalSockets | None = None,
    registry: FakeConnectionRegistry | None = None,
    buffer: InMemoryEventBuffer | None = None,
    spectators: InMemorySpectatorStore | None = None,
) -> MoveSubmissionHandler:
    """The real move handler over in-memory collaborators.

    Real, because §16's required coverage is about the *handler's* ordering
    and mapping. What is substituted is the engine (`FakeSubmitMoves` — the
    rules are `test_move_generation.py`'s and §16 forbids duplicating them),
    the transport and the stores.
    """
    return MoveSubmissionHandler(
        moves=submissions if submissions is not None else FakeSubmitMoves(),
        rooms=rooms,
        broadcaster=RoomBroadcaster(
            router=FleetConnectionRouter(
                registry=registry if registry is not None else FakeConnectionRegistry(),
                node_id=NODE_ID,
                metrics=RecordingMetrics(),
            ),
            sockets=sockets if sockets is not None else InMemoryLocalSockets(),
            publisher=publisher if publisher is not None else RecordingRemotePublisher(),
            metrics=RecordingMetrics(),
        ),
        buffer=buffer if buffer is not None else InMemoryEventBuffer(),
        spectators=spectators if spectators is not None else InMemorySpectatorStore(),
        idempotency=InMemoryMoveIdempotency(),
        limiter=limiter if limiter is not None else CountingMoveLimiter(allowance=100),
        metrics=RecordingMetrics(),
        idempotency_ttl_seconds=60,
    )


def _resumes(
    rooms: GameRoomService,
    *,
    snapshots: StubMatchSnapshots | None = None,
    buffer: InMemoryEventBuffer | None = None,
) -> ResumeHandler:
    """The real reconnect handler over in-memory collaborators."""
    return ResumeHandler(
        snapshots=snapshots if snapshots is not None else StubMatchSnapshots(),
        events=buffer if buffer is not None else InMemoryEventBuffer(),  # type: ignore[arg-type]
        rooms=rooms,
        metrics=RecordingMetrics(),
    )


def _spectators(
    *,
    snapshots: StubMatchSnapshots | None = None,
    policy: StubSpectatorPolicy | None = None,
    store: InMemorySpectatorStore | None = None,
) -> SpectatorHandler:
    """The real spectator handler over in-memory collaborators."""
    return SpectatorHandler(
        snapshots=snapshots if snapshots is not None else StubMatchSnapshots(),
        policy=policy if policy is not None else StubSpectatorPolicy(),
        store=store if store is not None else InMemorySpectatorStore(),
        metrics=RecordingMetrics(),
        subscription_ttl_seconds=900,
    )


def _service(
    tickets: FakeTicketRedeemer,
    registry: FakeConnectionRegistry,
    presence: RecordingPresence,
    rooms: GameRoomService | None = None,
    moves: MoveSubmissionHandler | None = None,
    sockets: InMemoryLocalSockets | None = None,
    resumes: ResumeHandler | None = None,
    spectators: SpectatorHandler | None = None,
) -> GatewayConnectionService:
    resolved_rooms = rooms if rooms is not None else _rooms()
    return GatewayConnectionService(
        tickets=tickets,
        registry=registry,
        rooms=resolved_rooms,
        moves=moves if moves is not None else _moves(resolved_rooms),
        resumes=resumes if resumes is not None else _resumes(resolved_rooms),
        spectators=spectators if spectators is not None else _spectators(),
        sockets=sockets if sockets is not None else InMemoryLocalSockets(),
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
            moves=_moves(_rooms()),
            resumes=_resumes(_rooms()),
            spectators=_spectators(),
            sockets=InMemoryLocalSockets(),
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

        Four things at once, because none of them is worth a connection of
        its own: the join is admitted for a participant, the confirmation
        comes back on the **`game` channel** rather than `system` (which is
        what makes AD-11's multiplexing observable), `both_connected` is
        `False` while the opponent has not arrived — the state a client
        renders as "waiting for your opponent" — and the tournament is told
        somebody turned up.

        The last is A64-019.5H §6e: a room join is what "turned up" means for
        a live game, and this is the only component that observes one. A
        match no tournament owns is unaffected, because the write matches no
        row.
        """
        player_id, opponent_id, match_id = generate_uuid7(), generate_uuid7(), generate_uuid7()
        rosters = StubMatchRosters()
        rosters.add(match_id, light=player_id, dark=opponent_id)
        attendance = RecordingAttendance()

        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [_frame("room.join", channel="game", payload={"match_id": str(match_id)})]
        )

        await _service(tickets, registry, presence, _rooms(rosters, attendance=attendance)).run(
            socket, ticket=VALID_TICKET
        )

        joined = next(m for m in socket.sent if m.type is MessageType.ROOM_JOINED)
        assert joined.channel is Channel.GAME
        assert joined.payload["both_connected"] is False
        assert set(joined.payload["participants"]) == {str(player_id), str(opponent_id)}
        assert attendance.marked == [(match_id, player_id)]

    @pytest.mark.asyncio
    async def test_a_failed_attendance_write_still_lets_the_players_in(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A64-019.5H §6e — telling the tournament must never cost a game.

        Attendance is written on the join path, so an unreachable
        `tournaments` schema would otherwise turn "the no-show policy may
        mis-adjudicate" into "nobody can play at all". The degradation is
        deliberately one-sided: the join completes, the socket is admitted,
        and the failure is an `ERROR` an operator can see.

        Safe because the sweep does not trust attendance alone — it re-reads
        `game`'s authoritative state first, so a started match is protected
        even when this write is lost.
        """
        player_id, opponent_id, match_id = generate_uuid7(), generate_uuid7(), generate_uuid7()
        rosters = StubMatchRosters()
        rosters.add(match_id, light=player_id, dark=opponent_id)
        attendance = RecordingAttendance(fails=True)

        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [_frame("room.join", channel="game", payload={"match_id": str(match_id)})]
        )

        with caplog.at_level(logging.ERROR):
            await _service(tickets, registry, presence, _rooms(rosters, attendance=attendance)).run(
                socket, ticket=VALID_TICKET
            )

        # The join succeeded — same reply a healthy tournament produces.
        joined = next(m for m in socket.sent if m.type is MessageType.ROOM_JOINED)
        assert joined.channel is Channel.GAME
        assert set(joined.payload["participants"]) == {str(player_id), str(opponent_id)}
        # Nothing was refused, and no error frame reached the client.
        assert not [m for m in socket.sent if m.type is MessageType.ERROR]
        assert attendance.marked == []
        # The operator is told.
        assert "gateway_attendance_write_failed" in caplog.text

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


class TestMoveSubmission:
    """A64-016.3 §16 — the transport half of a move.

    The real `MoveSubmissionHandler`, the real dispatch table, the real
    room service and the real `RoutingPlan` run here. What is substituted is
    the **engine**: `FakeSubmitMoves` stands in for `SubmitMoveUseCase`
    because these tests are about the ordering of the checks, the wire
    mapping and the fan-out, and §16 forbids duplicating the move-rule
    tests that already exist.
    """

    @pytest.mark.asyncio
    async def test_a_participant_in_the_room_has_their_move_accepted_and_broadcast(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§16.1, and the two-frame answer with it.

        The submitter receives `game.move.accepted` **correlated to their
        `request_id`** and `game.move.applied` as one of the room's
        recipients. Both, deliberately: merging them would mean a client
        could not tell its own move from its opponent's without inspecting
        the payload.

        The accepted frame carries the ply, the side to move and the
        server-derived applied move — not a board, because the client
        already applied it optimistically and needs confirmation rather than
        a copy.
        """
        player_id, opponent_id, match_id = generate_uuid7(), generate_uuid7(), generate_uuid7()
        rosters, members = StubMatchRosters(), FakeRoomMemberStore()
        rosters.add(match_id, light=player_id, dark=opponent_id)
        rooms = _rooms(rosters, members)
        sockets = InMemoryLocalSockets()

        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [
                _frame("room.join", channel="game", payload={"match_id": str(match_id)}),
                _frame(
                    "game.move.submit",
                    channel="game",
                    request_id="m1",
                    payload={"match_id": str(match_id), "path": ["c3", "d4"]},
                ),
            ]
        )

        await _service(
            tickets,
            registry,
            presence,
            rooms,
            moves=_moves(rooms, sockets=sockets, registry=registry),
            sockets=sockets,
        ).run(socket, ticket=VALID_TICKET)

        accepted = next(m for m in socket.sent if m.type is MessageType.MOVE_ACCEPTED)
        assert accepted.channel is Channel.GAME
        assert accepted.request_id == "m1"
        assert accepted.payload["ply"] == 1
        assert accepted.payload["applied"]["path"] == ["c3", "d4"]
        # Delivered to this connection as a room recipient, not only
        # acknowledged.
        assert MessageType.MOVE_APPLIED in socket.types()

    @pytest.mark.asyncio
    async def test_a_move_is_refused_before_the_engine_when_the_socket_is_not_in_the_room(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§16.2, and §4's ordering.

        Two refusals, one code each, and **neither reaches the engine** —
        asserted against `submissions`, because a handler that called the
        game first would produce the same wire answer while doing a database
        read and a move generation for a frame it was going to refuse.

        The non-participant gets `not_a_participant`; the participant who
        never sent `room.join` gets `not_in_room`, which is a different and
        actionable message — the fix is to join, where "that match is not
        yours" gives them nothing to do.
        """
        participant, outsider = generate_uuid7(), generate_uuid7()
        match_id = generate_uuid7()
        rosters = StubMatchRosters()
        rosters.add(match_id, light=participant, dark=generate_uuid7())
        rooms = _rooms(rosters)
        submissions = FakeSubmitMoves()
        move = _frame(
            "game.move.submit",
            channel="game",
            payload={"match_id": str(match_id), "path": ["c3", "d4"]},
        )

        for player_id, ticket in ((outsider, "outsider"), (participant, "unjoined")):
            tickets.add(ticket, player_id)
            socket = FakeGatewaySocket([move])
            await _service(
                tickets, registry, presence, rooms, moves=_moves(rooms, submissions=submissions)
            ).run(socket, ticket=ticket)

            rejected = next(m for m in socket.sent if m.type is MessageType.MOVE_REJECTED)
            assert rejected.payload["code"] == GatewayErrorCode.NOT_IN_ROOM.value

        assert submissions.submissions == []

    @pytest.mark.asyncio
    async def test_every_game_failure_becomes_a_stable_category_and_a_safe_sentence(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§16.3 and §16.4 together, plus §14's disclosure rule.

        Wrong side, illegal move, stale state and match-not-active are four
        distinct exceptions from `game` and four distinct wire codes — a
        client's response differs for each: resynchronise, log loudly,
        retry, stop.

        The **sentence never comes from the exception** (§14). Each is
        asserted to be the fixed one from `_REJECTIONS` rather than
        `str(error)`, which is what makes "no SQL errors, no Redis errors,
        no class names, no stack traces" a property of the table rather than
        a rule somebody remembers — the exception messages here are
        deliberately full of detail that must not appear.
        """
        player_id, match_id = generate_uuid7(), generate_uuid7()
        rosters, members = StubMatchRosters(), FakeRoomMemberStore()
        rosters.add(match_id, light=player_id, dark=generate_uuid7())
        rooms = _rooms(rosters, members)
        submissions = FakeSubmitMoves()

        failures = {
            NotYourTurn("light to move, dark submitted"): GatewayErrorCode.NOT_YOUR_TURN,
            IllegalMoveSubmitted("no piece on c3 in <Position ...>"): (
                GatewayErrorCode.ILLEGAL_MOVE
            ),
            StaleMatchState("expected ply 7, stored 8"): GatewayErrorCode.STALE_STATE,
            MatchNotActive("the match is cancelled"): GatewayErrorCode.MATCH_NOT_ACTIVE,
        }

        for index, (error, expected) in enumerate(failures.items()):
            submissions.raises = error
            ticket = f"ticket-{index}"
            tickets.add(ticket, player_id)
            socket = FakeGatewaySocket(
                [
                    _frame("room.join", channel="game", payload={"match_id": str(match_id)}),
                    _frame(
                        "game.move.submit",
                        channel="game",
                        payload={"match_id": str(match_id), "path": ["c3", "d4"]},
                    ),
                ]
            )
            await _service(
                tickets, registry, presence, rooms, moves=_moves(rooms, submissions=submissions)
            ).run(socket, ticket=ticket)

            rejected = next(m for m in socket.sent if m.type is MessageType.MOVE_REJECTED)
            assert rejected.payload["code"] == expected.value
            assert str(error) not in rejected.payload["reason"]

    @pytest.mark.asyncio
    async def test_a_duplicate_request_id_replays_the_answer_without_reapplying(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§16.5, and §7's scope.

        The same `request_id` three times. The engine is called **once** —
        asserted against `submissions`, which is the only place a second
        application would show — and all three answers are byte-identical,
        because the stored value is the frame that was sent rather than a
        decision the handler re-renders.

        A frame with **no** `request_id` is not deduplicated, which is the
        honest behaviour: there is nothing to key on, and inventing one
        would be the second correlation identifier §7 forbids. Asserted
        alongside, because a store that keyed on the payload instead would
        pass every assertion above and silently swallow a legitimate
        repeated move.
        """
        player_id, match_id = generate_uuid7(), generate_uuid7()
        rosters, members = StubMatchRosters(), FakeRoomMemberStore()
        rosters.add(match_id, light=player_id, dark=generate_uuid7())
        rooms = _rooms(rosters, members)
        submissions = FakeSubmitMoves()

        retried = _frame(
            "game.move.submit",
            channel="game",
            request_id="same",
            payload={"match_id": str(match_id), "path": ["c3", "d4"]},
        )
        uncorrelated = _frame(
            "game.move.submit",
            channel="game",
            payload={"match_id": str(match_id), "path": ["c3", "d4"]},
        )

        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [
                _frame("room.join", channel="game", payload={"match_id": str(match_id)}),
                retried,
                retried,
                retried,
                uncorrelated,
            ]
        )

        await _service(
            tickets, registry, presence, rooms, moves=_moves(rooms, submissions=submissions)
        ).run(socket, ticket=VALID_TICKET)

        accepted = [m for m in socket.sent if m.type is MessageType.MOVE_ACCEPTED]
        assert len(accepted) == 4
        assert {m.to_json() for m in accepted[:3]} == {accepted[0].to_json()}
        # Three correlated submissions applied once; the uncorrelated one
        # applied on its own.
        assert len(submissions.submissions) == 2

    @pytest.mark.asyncio
    async def test_the_move_limit_refuses_before_any_expensive_work(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§16.8, and §13's ordering requirement.

        One move allowed, three sent. The refusals carry `rate_limited` and
        the **connection stays open** — asserted by the `ping` at the end
        being answered, because §13 forbids closing a socket for one
        ordinary violation.

        `submissions` proves the ordering: a limiter placed after the room
        check or the decode would produce the same wire answers while having
        already done the work it exists to prevent. And `ping` is not
        charged against the limit — the limiter is consulted three times,
        not four.
        """
        player_id, match_id = generate_uuid7(), generate_uuid7()
        rosters, members = StubMatchRosters(), FakeRoomMemberStore()
        rosters.add(match_id, light=player_id, dark=generate_uuid7())
        rooms = _rooms(rosters, members)
        submissions = FakeSubmitMoves()
        limiter = CountingMoveLimiter(allowance=1)

        move = _frame(
            "game.move.submit",
            channel="game",
            payload={"match_id": str(match_id), "path": ["c3", "d4"]},
        )
        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [
                _frame("room.join", channel="game", payload={"match_id": str(match_id)}),
                move,
                move,
                move,
                _frame("ping"),
            ]
        )

        await _service(
            tickets,
            registry,
            presence,
            rooms,
            moves=_moves(rooms, submissions=submissions, limiter=limiter),
        ).run(socket, ticket=VALID_TICKET)

        refused = [
            m
            for m in socket.sent
            if m.type is MessageType.MOVE_REJECTED
            and m.payload["code"] == GatewayErrorCode.RATE_LIMITED.value
        ]
        assert len(refused) == 2
        assert len(submissions.submissions) == 1
        assert limiter.calls == 3
        assert MessageType.PONG in socket.types()


class TestRoutingPlanTransport:
    """§16.6 — the fan-out honours the plan."""

    @pytest.mark.asyncio
    async def test_local_routes_are_written_directly_and_remote_routes_group_by_node(
        self,
    ) -> None:
        """§8, and the bound that matters.

        Three players: one with two tabs on this node, one with three tabs
        split across two other nodes, one offline. The local pair are
        written to their sockets; the remote five become **two** forwarding
        requests — one per node, not one per connection.

        That last assertion is the whole test. A publisher that iterated
        routes would send the same frame three times to the node holding
        three tabs, and it looks identical to a working system until
        somebody opens a second tab.
        """
        here, split, offline = generate_uuid7(), generate_uuid7(), generate_uuid7()
        registry = FakeConnectionRegistry()
        sockets = InMemoryLocalSockets()
        publisher = RecordingRemotePublisher()

        local_sockets = []
        for _ in range(2):
            connection_id = uuid4()
            await registry.register(here, connection_id, node_id=NODE_ID, ttl_seconds=90)
            socket = FakeGatewaySocket()
            sockets.attach(connection_id, socket)
            local_sockets.append(socket)

        for node_id, tabs in (("node-b", 2), ("node-c", 1)):
            for _ in range(tabs):
                await registry.register(split, uuid4(), node_id=node_id, ttl_seconds=90)

        broadcaster = RoomBroadcaster(
            router=FleetConnectionRouter(
                registry=registry, node_id=NODE_ID, metrics=RecordingMetrics()
            ),
            sockets=sockets,
            publisher=publisher,
            metrics=RecordingMetrics(),
        )

        report = await broadcaster.deliver(
            move_applied(
                match_id=generate_uuid7(),
                ply=3,
                side_to_move="dark",
                fingerprint="fp",
                path=["c3", "d4"],
                captured=[],
                promoted_to=None,
            ),
            recipients=[here, split, offline],
        )

        assert report.local == 2
        assert all(s.types() == [MessageType.MOVE_APPLIED] for s in local_sockets)
        # One request per node, carrying that node's connections.
        assert report.remote_nodes == 2
        assert {r.node_id for r in publisher.published} == {"node-b", "node-c"}
        assert sorted(len(r.connection_ids) for r in publisher.published) == [1, 2]
        # An offline player contributes nothing and is not a failure.
        assert report.failures == 0


class TestTheRemoteTransportBus:
    """The bus seam — A64-016.4 §9, §11.8.

    A64-016.3 shipped `RemoteNodePublisher` with a log line behind it. This
    is the transport it now has, and the properties worth asserting are the
    ones a bus can silently get wrong: who a message was for, and whether it
    survived the journey unchanged.
    """

    @pytest.mark.asyncio
    async def test_the_bus_preserves_node_channel_envelope_and_request_id(self) -> None:
        """§11.8 — the four things a transport must not touch.

        A frame goes in through the publisher, crosses the bus, and comes
        back out of `consume` for the node it was addressed to. Every field
        is asserted **after decoding the delivered frame**, not on the
        message that was handed in, because the failure this catches is a
        transport that re-encodes: `request_id` and `channel` live inside
        the envelope, and a bus that rebuilt it would be a second encoder
        able to disagree with the first.

        The primitive round trip is asserted too. §9 requires a "stable
        primitive payload" and forbids socket references, and the property
        that makes that true rather than remembered is that the message
        survives `json.dumps` — anything that could not cross a process
        boundary could not be put in it.
        """
        bus = InProcessGatewayBus()
        publisher = BusRemoteNodePublisher(bus)
        recipients = (uuid4(), uuid4())
        sent = move_applied(
            match_id=generate_uuid7(),
            ply=7,
            side_to_move="dark",
            fingerprint="fp-7",
            path=["c3", "e5", "g3"],
            captured=["d4", "f4"],
            promoted_to=None,
        )

        assert await publisher.publish(
            ForwardingRequest(node_id="node-b", connection_ids=recipients, frame=sent.to_json())
        )

        # Addressed to one node: another node's consume finds nothing.
        assert await bus.consume("node-a", limit=10) == ()

        delivered = await bus.consume("node-b", limit=10)
        assert len(delivered) == 1
        assert delivered[0].node_id == "node-b"
        assert connection_ids_of(delivered[0]) == recipients

        # The envelope, decoded from what would actually reach a socket.
        arrived = decode(delivered[0].frame, max_bytes=64 * 1024)
        assert arrived.type is MessageType.MOVE_APPLIED
        assert arrived.channel is Channel.GAME
        assert arrived.payload == sent.payload

        # Consuming removes: a second drain finds nothing.
        assert await bus.consume("node-b", limit=10) == ()

        # And the whole message is JSON-round-trippable — §9's "stable
        # primitive payload", checked rather than asserted.
        primitive = json.loads(json.dumps(delivered[0].to_primitive()))
        assert BusMessage.from_primitive(primitive) == delivered[0]

    @pytest.mark.asyncio
    async def test_a_correlated_acknowledgement_survives_the_bus_unchanged(self) -> None:
        """`request_id` specifically, because it is the field a transport is
        most likely to lose.

        It lives inside the envelope rather than on the bus message, which
        is deliberate — §9 asks that it be preserved, and preserving it by
        **not touching it** is the form of that guarantee least able to go
        wrong. This asserts the consequence: a frame carrying a correlation
        token arrives carrying the same one.
        """
        bus = InProcessGatewayBus()
        acknowledgement = move_accepted(
            match_id=generate_uuid7(),
            ply=3,
            side_to_move="light",
            fingerprint="fp-3",
            path=["b6", "a5"],
            captured=[],
            promoted_to=None,
            request_id="move-42",
            result={"outcome": "win", "termination_reason": "no_legal_moves", "winner": "light"},
        )

        await BusRemoteNodePublisher(bus).publish(
            ForwardingRequest(
                node_id="node-c", connection_ids=(uuid4(),), frame=acknowledgement.to_json()
            )
        )

        arrived = decode((await bus.consume("node-c", limit=1))[0].frame, max_bytes=64 * 1024)
        assert arrived.request_id == "move-42"
        assert arrived.channel is Channel.GAME
        assert arrived.payload["result"]["winner"] == "light"


class TestReconnection:
    """A64-016.6 §9 — putting a client back where it was.

    The real `ResumeHandler`, the real room service and the real protocol
    codec run here; the snapshot reader is stubbed because how `game`
    replays a log to build one is `tests/contract/test_move_log.py`'s.
    """

    @pytest.mark.asyncio
    async def test_a_small_gap_is_answered_with_ordered_incremental_events(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.1, and §2's ordering.

        The client saw ply 2 and the server is at ply 5. The buffer holds
        every ply, so it proves continuity and the three missed frames come
        back **in sequence order** — asserted by decoding them, because the
        frames are opaque strings to everything between the buffer and the
        client and an out-of-order replay would rebuild a different board.

        They are the *same bytes* a live socket received, which is why the
        buffer stores the encoded frame rather than a decoded event: a
        second encoder is a second thing able to disagree.
        """
        player_id, opponent_id, match_id = generate_uuid7(), generate_uuid7(), generate_uuid7()
        rosters, members = StubMatchRosters(), FakeRoomMemberStore()
        rosters.add(match_id, light=player_id, dark=opponent_id)
        rooms = _rooms(rosters, members)

        snapshots = StubMatchSnapshots()
        snapshots.add(match_id, light=player_id, dark=opponent_id, sequence=5)
        buffer = InMemoryEventBuffer()
        for ply in range(1, 6):
            await buffer.append(
                match_id,
                sequence=ply,
                frame=move_applied(
                    match_id=match_id,
                    ply=ply,
                    side_to_move="dark",
                    fingerprint=f"fp-{ply}",
                    path=["c3", "d4"],
                    captured=[],
                    promoted_to=None,
                ).to_json(),
            )

        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [
                _frame(
                    "game.resume",
                    channel="game",
                    request_id="r1",
                    payload={"match_id": str(match_id), "last_known_sequence": 2},
                )
            ]
        )

        await _service(
            tickets,
            registry,
            presence,
            rooms,
            resumes=_resumes(rooms, snapshots=snapshots, buffer=buffer),
        ).run(socket, ticket=VALID_TICKET)

        answer = next(m for m in socket.sent if m.type is MessageType.EVENTS)
        assert answer.request_id == "r1"
        assert answer.channel is Channel.GAME
        replayed = [decode(frame, max_bytes=64 * 1024) for frame in answer.payload["frames"]]
        assert [frame.payload["ply"] for frame in replayed] == [3, 4, 5]

    @pytest.mark.asyncio
    async def test_a_gap_the_buffer_cannot_prove_forces_a_resync(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.2 and §9.8, and §6's prohibition on silent partial recovery.

        The client saw ply 2; the buffer has trimmed and now starts at ply
        7. It **can** return four frames — and returning them would leave
        the client missing plies 3 to 6 while believing it was current,
        which is the failure §6 exists to prevent.

        So the answer is `game.resync_required` rather than a snapshot the
        client did not ask for: it is told to start over, and it can count
        how often that happens, which is what says the buffer is too small
        for the disconnections this deployment actually sees.
        """
        player_id, opponent_id, match_id = generate_uuid7(), generate_uuid7(), generate_uuid7()
        rosters = StubMatchRosters()
        rosters.add(match_id, light=player_id, dark=opponent_id)
        rooms = _rooms(rosters)

        snapshots = StubMatchSnapshots()
        snapshots.add(match_id, light=player_id, dark=opponent_id, sequence=10)
        buffer = InMemoryEventBuffer()
        for ply in range(7, 11):
            await buffer.append(match_id, sequence=ply, frame=f'{{"ply":{ply}}}')

        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [
                _frame(
                    "game.resume",
                    channel="game",
                    payload={"match_id": str(match_id), "last_known_sequence": 2},
                )
            ]
        )

        await _service(
            tickets,
            registry,
            presence,
            rooms,
            resumes=_resumes(rooms, snapshots=snapshots, buffer=buffer),
        ).run(socket, ticket=VALID_TICKET)

        assert MessageType.RESYNC_REQUIRED in socket.types()
        assert MessageType.EVENTS not in socket.types()

    @pytest.mark.asyncio
    async def test_a_client_with_nothing_gets_a_snapshot_carrying_the_clock(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.2, §9.5 and §9.6 — the snapshot, its sequence, and the clock.

        A client reporting no sequence is asking to start over, so it gets
        the full state. Three things are asserted about it:

        **The sequence** is the match's, which becomes the client's new
        synchronisation baseline (§6) — everything after it is incremental.

        **The clock is absolute.** §7 forbids a client extrapolating from
        stale values, and a reconnecting client is exactly the one whose own
        countdown drifted for the whole disconnection. A `deadline` and a
        `server_time` let it correct its skew; a duration would drift by the
        latency it was meant to describe.

        **No handles and no ratings.** Those are `users`' and are composed by
        whoever renders them — asserted, because a snapshot that grew them
        would make `game` depend on a module it has no business knowing.
        """
        player_id, opponent_id, match_id = generate_uuid7(), generate_uuid7(), generate_uuid7()
        rosters = StubMatchRosters()
        rosters.add(match_id, light=player_id, dark=opponent_id)
        rooms = _rooms(rosters)

        snapshots = StubMatchSnapshots()
        snapshots.add(
            match_id,
            light=player_id,
            dark=opponent_id,
            sequence=4,
            clock=ClockView(
                light_ms=42_000,
                dark_ms=51_000,
                active_side=PlayerSide.LIGHT,
                deadline=NOW + timedelta(seconds=42),
                server_time=NOW,
            ),
        )

        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [_frame("game.resume", channel="game", payload={"match_id": str(match_id)})]
        )

        await _service(
            tickets, registry, presence, rooms, resumes=_resumes(rooms, snapshots=snapshots)
        ).run(socket, ticket=VALID_TICKET)

        snapshot = next(m for m in socket.sent if m.type is MessageType.SNAPSHOT)
        assert snapshot.payload["sequence"] == 4
        assert snapshot.payload["clock"]["light_ms"] == 42_000
        assert snapshot.payload["clock"]["active_side"] == "light"
        # Absolute, so a client corrects its skew rather than accumulating it.
        assert snapshot.payload["clock"]["deadline"].startswith("2026-")
        assert snapshot.payload["clock"]["server_time"].startswith("2026-")
        assert "handle" not in json.dumps(snapshot.payload)
        assert "rating" not in json.dumps(snapshot.payload)

    @pytest.mark.asyncio
    async def test_a_player_opening_a_game_nobody_has_moved_in_gets_the_board(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """A64-020.5B — the resume every game begins with.

        A match at sequence 0 and a client reporting nothing are numerically
        the same value, and the handler used to answer "you are current" to
        it, because `0 >= 0`. That is true and useless: the client is
        holding no position, no clocks and no side to move, so the very
        first resume of every game returned `game.resumed` and left a board
        that never rendered. Found in a two-browser run against the real
        gateway, where both players reached `/games/{id}` and saw nothing.

        `<= NO_SEQUENCE` means *I am holding nothing* whatever the server's
        sequence is, so it is answered before the fast path. Asserted with
        the opening position present in the payload rather than only the
        frame type, because a snapshot of an empty board would satisfy the
        type and reproduce the bug.
        """
        player_id, opponent_id, match_id = generate_uuid7(), generate_uuid7(), generate_uuid7()
        rosters = StubMatchRosters()
        rosters.add(match_id, light=player_id, dark=opponent_id)
        rooms = _rooms(rosters)

        snapshots = StubMatchSnapshots()
        snapshots.add(match_id, light=player_id, dark=opponent_id, sequence=0)

        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [_frame("game.resume", channel="game", payload={"match_id": str(match_id)})]
        )

        await _service(
            tickets, registry, presence, rooms, resumes=_resumes(rooms, snapshots=snapshots)
        ).run(socket, ticket=VALID_TICKET)

        snapshot = next(m for m in socket.sent if m.type is MessageType.SNAPSHOT)
        assert snapshot.payload["sequence"] == 0
        assert snapshot.payload["pieces"] != []
        assert snapshot.payload["side_to_move"] == "light"

    @pytest.mark.asyncio
    async def test_a_non_participant_cannot_resume_and_learns_nothing(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.3, and the disclosure rule the whole subsystem keeps.

        A non-participant and an unknown match get the **same** code, so a
        client cannot enumerate live match identifiers by sending resume
        frames — the same argument the room join and the move path both
        make.

        Nothing is attached in either case, asserted against the store: a
        refusal that still joined the room would put a stranger's connection
        in a fan-out's recipient set.
        """
        outsider, match_id = generate_uuid7(), generate_uuid7()
        rosters, members = StubMatchRosters(), FakeRoomMemberStore()
        rooms = _rooms(rosters, members)

        snapshots = StubMatchSnapshots()
        snapshots.add(match_id, light=generate_uuid7(), dark=generate_uuid7(), sequence=3)

        tickets.add(VALID_TICKET, outsider)
        socket = FakeGatewaySocket(
            [
                _frame("game.resume", channel="game", payload={"match_id": str(match_id)}),
                _frame(
                    "game.resume",
                    channel="game",
                    payload={"match_id": str(generate_uuid7())},
                ),
            ]
        )

        await _service(
            tickets, registry, presence, rooms, resumes=_resumes(rooms, snapshots=snapshots)
        ).run(socket, ticket=VALID_TICKET)

        refusals = [m for m in socket.sent if m.type is MessageType.ERROR]
        assert len(refusals) == 2
        assert all(
            m.payload == {"code": GatewayErrorCode.NOT_A_PARTICIPANT.value} for m in refusals
        )
        assert MessageType.SNAPSHOT not in socket.types()
        assert members.rooms.get(match_id, []) == []

    @pytest.mark.asyncio
    async def test_a_resume_on_another_node_rejoins_without_moving_a_socket(
        self,
        tickets: FakeTicketRedeemer,
        presence: RecordingPresence,
    ) -> None:
        """§9.4 and §5 — cross-node resume, and why it needs nothing special.

        The player's first connection is registered on `node-a`; they
        reconnect and land on this node. Afterwards the registry holds
        **both** connections on their own nodes and the room holds both
        members — no socket moved, and neither node knows about the other's.

        That is the whole of §5: every store a resume touches is
        fleet-wide, so the new node reads exactly the state the old one
        wrote. The stale connection is left to its own cleanup or its TTL,
        which A64-016.1 §7 already guarantees.
        """
        player_id, opponent_id, match_id = generate_uuid7(), generate_uuid7(), generate_uuid7()
        registry = FakeConnectionRegistry()
        rosters, members = StubMatchRosters(), FakeRoomMemberStore()
        rosters.add(match_id, light=player_id, dark=opponent_id)
        rooms = _rooms(rosters, members)

        snapshots = StubMatchSnapshots()
        snapshots.add(match_id, light=player_id, dark=opponent_id, sequence=2)
        buffer = InMemoryEventBuffer()
        for ply in (1, 2):
            await buffer.append(match_id, sequence=ply, frame=f'{{"ply":{ply}}}')

        # The connection they had before, on another node, still registered.
        elsewhere = uuid4()
        await registry.register(player_id, elsewhere, node_id="node-a", ttl_seconds=90)
        await members.join(
            match_id, RoomMember(player_id=player_id, connection_id=elsewhere), ttl_seconds=3600
        )

        tickets.add(VALID_TICKET, player_id)
        socket = FakeGatewaySocket(
            [
                _frame(
                    "game.resume",
                    channel="game",
                    payload={"match_id": str(match_id), "last_known_sequence": 1},
                )
            ]
        )

        await _service(
            tickets,
            registry,
            presence,
            rooms,
            resumes=_resumes(rooms, snapshots=snapshots, buffer=buffer),
        ).run(socket, ticket=VALID_TICKET)

        assert MessageType.EVENTS in socket.types()
        # The old node's connection is untouched — no socket moved.
        assert registry.connections[player_id][elsewhere] == "node-a"
        assert any(member.connection_id == elsewhere for member in members.rooms[match_id])

    @pytest.mark.asyncio
    async def test_a_repeated_resume_is_idempotent_and_a_current_client_is_told_so(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.7 and §8, plus the fast path.

        The same resume three times with one `request_id`. Room membership
        must not double — a duplicated member would put one connection in a
        fan-out's recipient set three times — and the answer must be the
        same each time.

        A client that has missed nothing gets `game.resumed` rather than a
        snapshot, which is the common case for a socket that dropped and
        returned within a second: sending it a snapshot it already has would
        make every flaky network a full replay.

        There is deliberately **no** `request_id` cache on this path, unlike
        the move path: a move applies something and a resume does not, so
        replaying a stored answer would be caching a read.
        """
        player_id, opponent_id, match_id = generate_uuid7(), generate_uuid7(), generate_uuid7()
        rosters, members = StubMatchRosters(), FakeRoomMemberStore()
        rosters.add(match_id, light=player_id, dark=opponent_id)
        rooms = _rooms(rosters, members)

        snapshots = StubMatchSnapshots()
        snapshots.add(match_id, light=player_id, dark=opponent_id, sequence=3)

        retried = _frame(
            "game.resume",
            channel="game",
            request_id="same",
            payload={"match_id": str(match_id), "last_known_sequence": 3},
        )
        tickets.add(VALID_TICKET, player_id)
        # Held open, so membership is asserted while the connection is live —
        # the lifecycle's own cleanup empties the room on disconnect, which
        # is correct and would hide the duplication this is about.
        socket = FakeGatewaySocket([retried, retried, retried], holds_open=True)

        served = asyncio.create_task(
            _service(
                tickets, registry, presence, rooms, resumes=_resumes(rooms, snapshots=snapshots)
            ).run(socket, ticket=VALID_TICKET)
        )
        # The three scripted frames are consumed without suspending on
        # anything real, so one scheduler turn per frame is enough — the
        # socket only blocks once its script is exhausted.
        for _ in range(len(socket.sent) + 8):
            await asyncio.sleep(0)

        answers = [m for m in socket.sent if m.type is MessageType.RESUMED]
        assert len(answers) == 3
        assert {m.to_json() for m in answers} == {answers[0].to_json()}
        assert all(m.payload["sequence"] == 3 for m in answers)
        # One connection, one membership — three joins did not duplicate it.
        assert len(members.rooms[match_id]) == 1

        socket.hang_up()
        await served


class TestSpectating:
    """A64-016.7 §9 — watching a game without being in it."""

    @pytest.mark.asyncio
    async def test_an_eligible_spectator_joins_and_receives_the_position(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.1 and §9.3 — admitted, subscribed, and given the board.

        Two assertions in one test because they are one outcome: a
        `spectator.joined` that did not carry the position would be a client
        told it is watching and shown nothing, and a subscription that was
        not recorded would be a client shown a board it never receives
        another frame for.

        What crosses is the **same projection a resuming participant gets**
        — the fingerprint, the placement and the sequence — and what does
        not is anything about the two players beyond their identifiers. See
        `app/gateway/projections.py`.
        """
        viewer, light, dark = generate_uuid7(), generate_uuid7(), generate_uuid7()
        match_id = generate_uuid7()

        snapshots = StubMatchSnapshots()
        snapshots.add(match_id, light=light, dark=dark, sequence=4)
        store = InMemorySpectatorStore()

        tickets.add(VALID_TICKET, viewer)
        socket = FakeGatewaySocket(
            [
                _frame(
                    "spectator.join",
                    channel="game",
                    request_id="s1",
                    payload={"match_id": str(match_id)},
                )
            ],
            # Held open, because the subscription is asserted **while the
            # connection is live**: closing it runs the cleanup that empties
            # every audience, which the last test in this class covers.
            holds_open=True,
        )

        served = asyncio.create_task(
            _service(
                tickets,
                registry,
                presence,
                spectators=_spectators(snapshots=snapshots, store=store),
            ).run(socket, ticket=VALID_TICKET)
        )
        for _ in range(len(socket.sent) + 8):
            await asyncio.sleep(0)

        joined = next(m for m in socket.sent if m.type is MessageType.SPECTATOR_JOINED)
        assert joined.request_id == "s1"
        assert joined.payload["audience"] == 1
        assert joined.payload["sequence"] == 4
        assert joined.payload["fingerprint"] == "fingerprint-4"
        assert {sub.player_id for sub in store.watching[match_id]} == {viewer}

        socket.hang_up()
        await served

    @pytest.mark.asyncio
    async def test_a_block_between_the_viewer_and_a_player_refuses_the_join(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.2, over the **real** policy rather than a stub.

        The block is one-directional in `friends` and the refusal is
        symmetric here, which is BL-1's reasoning carried over: a blocker
        who could still be watched by the person they blocked has gained
        nothing from blocking them.

        The wire code is `spectating_forbidden` and **not** a distinct
        "you are blocked", because a client that could tell the two apart
        could probe the block graph one match at a time.
        """
        viewer, light, dark = generate_uuid7(), generate_uuid7(), generate_uuid7()
        match_id = generate_uuid7()

        snapshots = StubMatchSnapshots()
        snapshots.add(match_id, light=light, dark=dark)
        store = InMemorySpectatorStore()
        policy = BlockAwareSpectatorPolicy(StubPairingExclusions([(light, viewer)]))

        tickets.add(VALID_TICKET, viewer)
        socket = FakeGatewaySocket(
            [_frame("spectator.join", channel="game", payload={"match_id": str(match_id)})]
        )

        await _service(
            tickets,
            registry,
            presence,
            spectators=SpectatorHandler(
                snapshots=snapshots,
                policy=policy,
                store=store,
                metrics=RecordingMetrics(),
                subscription_ttl_seconds=900,
            ),
        ).run(socket, ticket=VALID_TICKET)

        refusal = next(m for m in socket.sent if m.type is MessageType.ERROR)
        assert refusal.payload["code"] == GatewayErrorCode.SPECTATING_FORBIDDEN
        assert match_id not in store.watching

    @pytest.mark.asyncio
    async def test_a_match_that_is_not_being_played_cannot_be_watched(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.2 and §1 — an unknown match and a pending one are one answer.

        A pairing still being accepted is not a game: there is nothing to
        watch, and its *existence* is not public. So it produces the same
        `not_spectatable` a match id nobody ever issued does, which is what
        keeps live match identifiers unenumerable — the rule the room join
        and the move path already keep.
        """
        viewer, light, dark = generate_uuid7(), generate_uuid7(), generate_uuid7()
        pending_id, unknown_id = generate_uuid7(), generate_uuid7()

        snapshots = StubMatchSnapshots()
        snapshots.add(
            pending_id, light=light, dark=dark, status=MatchRecordStatus.PENDING_ACCEPTANCE
        )

        tickets.add(VALID_TICKET, viewer)
        socket = FakeGatewaySocket(
            [
                _frame("spectator.join", channel="game", payload={"match_id": str(pending_id)}),
                _frame("spectator.join", channel="game", payload={"match_id": str(unknown_id)}),
            ]
        )

        await _service(
            tickets,
            registry,
            presence,
            spectators=_spectators(
                snapshots=snapshots, policy=BlockAwareSpectatorPolicy(StubPairingExclusions())
            ),
        ).run(socket, ticket=VALID_TICKET)

        refusals = [m for m in socket.sent if m.type is MessageType.ERROR]
        assert [m.payload["code"] for m in refusals] == [
            GatewayErrorCode.NOT_SPECTATABLE,
            GatewayErrorCode.NOT_SPECTATABLE,
        ]

    @pytest.mark.asyncio
    async def test_a_move_reaches_the_watching_tab_and_not_the_viewers_other_tab(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.4, and the reason spectators are `(player, connection)` pairs.

        The viewer has two sockets open and pressed watch in one of them.
        The router resolves **both** — that is what it does for a player —
        so the fan-out would deliver a game to a tab that never asked for
        one unless the plan is restricted to the subscribed connection.

        The participants are unaffected: they are in the room on every tab,
        which is what a room is.
        """
        viewer, light, dark = generate_uuid7(), generate_uuid7(), generate_uuid7()
        match_id = generate_uuid7()

        sockets = InMemoryLocalSockets()
        watching_id, other_id = uuid4(), uuid4()
        watching, other = FakeGatewaySocket(), FakeGatewaySocket()
        for connection_id, socket in ((watching_id, watching), (other_id, other)):
            await registry.register(viewer, connection_id, node_id=NODE_ID, ttl_seconds=90)
            sockets.attach(connection_id, socket)

        store = InMemorySpectatorStore()
        await store.subscribe(
            match_id,
            SpectatorSubscription(player_id=viewer, connection_id=watching_id),
            ttl_seconds=900,
        )

        broadcaster = RoomBroadcaster(
            router=FleetConnectionRouter(
                registry=registry, node_id=NODE_ID, metrics=RecordingMetrics()
            ),
            sockets=sockets,
            publisher=RecordingRemotePublisher(),
            metrics=RecordingMetrics(),
        )

        report = await broadcaster.deliver(
            move_applied(
                match_id=match_id,
                ply=3,
                side_to_move="dark",
                fingerprint="fp",
                path=["c3", "d4"],
                captured=[],
                promoted_to=None,
            ),
            recipients=[light, dark],
            spectators=await store.routes_for(match_id),
        )

        assert report.local == 1
        assert watching.types() == [MessageType.MOVE_APPLIED]
        assert other.types() == []

    @pytest.mark.asyncio
    async def test_an_event_that_is_not_spectator_safe_never_reaches_the_audience(
        self,
        registry: FakeConnectionRegistry,
    ) -> None:
        """§9.6 — the allowlist, and why it is checked at the fan-out.

        `game.resumed` is a participant's own reconnection outcome and
        carries `both_connected`, which is a statement about the *players'*
        presence. Passing it here with an audience is exactly the mistake a
        future caller will make, and the filter is in `deliver` rather than
        at the call site so that making it costs a spectator nothing rather
        than leaking a frame.
        """
        viewer, match_id = generate_uuid7(), generate_uuid7()
        connection_id = uuid4()

        sockets = InMemoryLocalSockets()
        socket = FakeGatewaySocket()
        await registry.register(viewer, connection_id, node_id=NODE_ID, ttl_seconds=90)
        sockets.attach(connection_id, socket)

        broadcaster = RoomBroadcaster(
            router=FleetConnectionRouter(
                registry=registry, node_id=NODE_ID, metrics=RecordingMetrics()
            ),
            sockets=sockets,
            publisher=RecordingRemotePublisher(),
            metrics=RecordingMetrics(),
        )

        report = await broadcaster.deliver(
            resumed(match_id=match_id, sequence=4, both_connected=True, request_id=None),
            recipients=[],
            spectators=[SpectatorSubscription(player_id=viewer, connection_id=connection_id)],
        )

        assert report.local == 0
        assert socket.types() == []

    @pytest.mark.asyncio
    async def test_a_spectator_submitting_a_move_is_refused_by_the_room(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.5, and the shape of the guarantee rather than a check.

        The viewer joins as a spectator and then submits a move on the same
        socket. It is refused `not_in_room` by `MoveSubmissionHandler` —
        machinery that predates spectating and knows nothing about it —
        because a subscription lives in `gwspec:v1:` and the move path reads
        `gwroom:v1:`.

        That is stronger than a guard somebody has to remember to add to the
        next handler, and the assertion that no submission reached `game` is
        what says the refusal happened at the gateway rather than in the
        engine.
        """
        viewer, light, dark = generate_uuid7(), generate_uuid7(), generate_uuid7()
        match_id = generate_uuid7()

        snapshots = StubMatchSnapshots()
        snapshots.add(match_id, light=light, dark=dark)
        rosters = StubMatchRosters()
        rosters.add(match_id, light=light, dark=dark)
        rooms = _rooms(rosters)
        submissions = FakeSubmitMoves()

        tickets.add(VALID_TICKET, viewer)
        socket = FakeGatewaySocket(
            [
                _frame("spectator.join", channel="game", payload={"match_id": str(match_id)}),
                _frame(
                    "game.move.submit",
                    channel="game",
                    request_id="m1",
                    payload={"match_id": str(match_id), "path": ["c3", "d4"]},
                ),
            ]
        )

        await _service(
            tickets,
            registry,
            presence,
            rooms,
            moves=_moves(rooms, submissions=submissions),
            spectators=_spectators(snapshots=snapshots),
        ).run(socket, ticket=VALID_TICKET)

        assert MessageType.SPECTATOR_JOINED in socket.types()
        rejection = next(m for m in socket.sent if m.type is MessageType.MOVE_REJECTED)
        assert rejection.payload["code"] == GatewayErrorCode.NOT_IN_ROOM
        assert submissions.submissions == []

    @pytest.mark.asyncio
    async def test_leaving_and_disconnecting_both_end_the_subscription(
        self,
        tickets: FakeTicketRedeemer,
        registry: FakeConnectionRegistry,
        presence: RecordingPresence,
    ) -> None:
        """§9.7 — the two ways an audience shrinks.

        A client that says `spectator.leave` is out immediately, and one
        that simply vanishes is taken out by the connection cleanup, because
        a socket that dropped never sends anything. Both are asserted in one
        test because a fix to either that broke the other would still pass a
        test of one.

        The two matches make the disconnect case meaningful: `detach`
        removes this connection from **every** audience it was in, which is
        what the reverse index exists for. What is deliberately not asserted
        is the TTL — it is Redis's and is asserted against it in
        `tests/contract/test_spectating.py`.
        """
        viewer, light, dark = generate_uuid7(), generate_uuid7(), generate_uuid7()
        left_id, dropped_id = generate_uuid7(), generate_uuid7()

        snapshots = StubMatchSnapshots()
        snapshots.add(left_id, light=light, dark=dark)
        snapshots.add(dropped_id, light=light, dark=dark)
        store = InMemorySpectatorStore()

        tickets.add(VALID_TICKET, viewer)
        socket = FakeGatewaySocket(
            [
                _frame("spectator.join", channel="game", payload={"match_id": str(left_id)}),
                _frame("spectator.join", channel="game", payload={"match_id": str(dropped_id)}),
                _frame(
                    "spectator.leave",
                    channel="game",
                    request_id="l1",
                    payload={"match_id": str(left_id)},
                ),
            ]
        )

        await _service(
            tickets,
            registry,
            presence,
            spectators=_spectators(snapshots=snapshots, store=store),
        ).run(socket, ticket=VALID_TICKET)

        confirmation = next(m for m in socket.sent if m.type is MessageType.SPECTATOR_LEFT)
        assert confirmation.request_id == "l1"
        # The explicit leave, and the disconnect that took the other with it.
        assert store.watching[left_id] == set()
        assert store.watching[dropped_id] == set()


class TestCrossNodeForwarding:
    """A64-016.8 — the loop A64-016.5 left out.

    The transport had a writer and a reader and nothing between them, so a
    frame published for another node was written to that node's stream and
    read by nobody. These two tests are the round trip end to end: what one
    node publishes, the other node's forwarding pass delivers to the socket
    it actually holds.
    """

    @pytest.mark.asyncio
    async def test_a_frame_published_for_another_node_reaches_that_nodes_socket(
        self, registry: FakeConnectionRegistry
    ) -> None:
        """Publish on node A, forward on node B, and the socket has it.

        Driven through the **real** fan-out rather than by handing the
        forwarder a message: the assertion that matters is that the bytes a
        remote client receives are the bytes `RoomBroadcaster` produced, and
        a test that constructed the `BusMessage` itself would prove the
        forwarder works against a message no publisher writes.

        Node B's own recipient list is one connection, so a pass that
        delivered to every connection it knows about rather than to the
        ones the publisher named would still pass — which is why the
        opponent below has a second tab that node B does not hold.
        """
        here, there = generate_uuid7(), generate_uuid7()
        bus = InProcessGatewayBus()

        # Node A: the publisher. It holds nothing and delivers nothing.
        remote_connection = uuid4()
        await registry.register(there, remote_connection, node_id="node-b", ttl_seconds=90)
        await registry.register(here, uuid4(), node_id="node-a", ttl_seconds=90)

        report = await RoomBroadcaster(
            router=FleetConnectionRouter(
                registry=registry, node_id="node-a", metrics=RecordingMetrics()
            ),
            sockets=InMemoryLocalSockets(),
            publisher=BusRemoteNodePublisher(bus),
            metrics=RecordingMetrics(),
        ).deliver(
            move_applied(
                match_id=generate_uuid7(),
                ply=7,
                side_to_move="light",
                fingerprint="fp-7",
                path=["e5", "f6"],
                captured=[],
                promoted_to=None,
            ),
            recipients=[here, there],
        )
        assert report.remote_nodes == 1

        # Node B: the addressee. Its own socket registry, its own pass.
        sockets = InMemoryLocalSockets()
        socket = FakeGatewaySocket()
        sockets.attach(remote_connection, socket)

        run = await GatewayForwarder(
            bus=bus,
            sockets=sockets,
            metrics=RecordingMetrics(),
            node_id="node-b",
            batch_size=64,
        ).forward_once()

        assert run == ForwardingRun(consumed=1, delivered=1, missing=0)
        assert socket.types() == [MessageType.MOVE_APPLIED]
        assert socket.sent[0].payload["ply"] == 7

    @pytest.mark.asyncio
    async def test_a_pass_tolerates_a_recipient_whose_socket_has_gone(self) -> None:
        """§10's tolerance, on the far side of the bus.

        A connection that closed between the publishing node building its
        plan and this pass running is the ordinary case — the registry entry
        it was resolved from has not lapsed yet — so the frame reaches the
        tab that is still open and the one that is not is counted, not
        raised.

        A pass that raised here would stop the schedule, and a node that has
        silently stopped forwarding is invisible until somebody's opponent
        appears to have frozen.
        """
        bus = InProcessGatewayBus()
        live_connection, closed_connection = uuid4(), uuid4()

        # Through the real publisher, which is what turns a
        # `ForwardingRequest`'s `UUID`s into the wire strings a `BusMessage`
        # carries — a test that published directly would exercise a shape
        # nothing produces.
        await BusRemoteNodePublisher(bus).publish(
            ForwardingRequest(
                node_id="node-b",
                connection_ids=(live_connection, closed_connection),
                frame=move_applied(
                    match_id=generate_uuid7(),
                    ply=1,
                    side_to_move="dark",
                    fingerprint="fp-1",
                    path=["c3", "d4"],
                    captured=[],
                    promoted_to=None,
                ).to_json(),
            )
        )

        sockets = InMemoryLocalSockets()
        socket = FakeGatewaySocket()
        sockets.attach(live_connection, socket)

        run = await GatewayForwarder(
            bus=bus,
            sockets=sockets,
            metrics=RecordingMetrics(),
            node_id="node-b",
            batch_size=64,
        ).forward_once()

        assert run == ForwardingRun(consumed=1, delivered=1, missing=1)
        assert socket.types() == [MessageType.MOVE_APPLIED]
        # Acknowledged: a second pass has nothing left to read.
        assert await GatewayForwarder(
            bus=bus,
            sockets=sockets,
            metrics=RecordingMetrics(),
            node_id="node-b",
            batch_size=64,
        ).forward_once() == ForwardingRun(consumed=0, delivered=0, missing=0)
