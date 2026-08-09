"""In-memory stand-ins for the gateway's four ports — A64-016.1.

What is faked is **transport and storage**, never the thing under test.
`GatewayConnectionService`, the real envelope codec and the real presence
port all run against these, so the lifecycle, the presence transitions and
the cleanup guarantees are genuinely exercised.

## What `FakeConnectionRegistry` models, and why

It returns the live count **from** `register` and `unregister`, which is the
property the real adapter buys with a `MULTI`/`EXEC` transaction and which
the whole presence transition depends on — "was I the first", "am I the
last". Modelled because a fake that made the service read the count
separately would leave the service's actual arrangement untested.

What is **not** modelled is the atomicity. Two nodes racing against real
Redis resolve inside one transaction; here they would interleave. That
property belongs to Redis and is asserted against it, for the same reason
`tests/fakes/queue_repository.py` declines to reimplement `SKIP LOCKED` —
except that no contract suite covers this keyspace yet, which is recorded in
A64-016.1's report rather than pretended away.

`FakeGatewaySocket` is scripted rather than interactive: a test hands it the
frames a client would send and reads back what the server wrote. That is
enough for every rule in this task because none of them depends on timing
between the two directions, and it keeps a lifecycle test to a list of
strings instead of an event loop with a client in it.
"""

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from app.gateway.event_buffer import BufferedEvents
from app.gateway.ports import ConnectionClosed, ConnectionRoute, ForwardingRequest
from app.gateway.protocol import GatewayMessage, MessageType
from app.gateway.rooms import RoomMember, RoomProgress
from app.gateway.spectators import SpectatorRefusal, SpectatorSubscription
from app.modules.engine import PlayerSide

# The **domain** outcome, not `game.public`'s metric-label enum of the same
# name — `GameCommandResult.outcome` is typed with this one, exactly as
# `MatchSnapshot.outcome` is.
from app.modules.game.domain.result import MatchOutcome
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.public import (
    AppliedMove,
    ClockView,
    DrawAgreementView,
    DrawOfferState,
    DrawOfferView,
    GameCommand,
    GameCommandRequest,
    GameCommandResult,
    MatchRecordStatus,
    MatchRoster,
    MatchSnapshot,
    PlacedPiece,
    SubmitMoveRequest,
    SubmitMoveResult,
    TerminationReason,
)
from app.modules.users.public import DeviceType

#: The instant the command fake stamps. Fixed, so a test asserting an
#: offer's timestamp asserts a value rather than "close to now".
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeGatewaySocket:
    """A `GatewaySocket` that replays a script and records what it was sent.

    `receive` yields each scripted frame in turn and then raises
    `ConnectionClosed`, which is exactly what a real client hanging up looks
    like — so a test ends a connection by running out of script rather than
    by arranging a disconnect.
    """

    def __init__(self, frames: Sequence[str] = (), *, holds_open: bool = False) -> None:
        self._inbound = list(frames)
        self.sent: list[GatewayMessage] = []
        self.closed_with: tuple[int, str] | None = None
        #: Makes `send` raise, so the "peer went away mid-write" path is
        #: exercised rather than asserted.
        self.send_fails = False
        #: When set, `receive` blocks after the script rather than raising —
        #: a connection that is genuinely still open. `hang_up()` ends it.
        #:
        #: Needed because the multi-connection rule is about a *concurrent*
        #: state ("one closes while another remains active"), and a socket
        #: that always disconnects at the end of its script cannot express
        #: one connection outliving another.
        self._hangup = asyncio.Event() if holds_open else None

    async def send(self, message: GatewayMessage) -> None:
        if self.send_fails:
            raise ConnectionClosed
        self.sent.append(message)

    async def receive(self) -> str:
        if self._inbound:
            return self._inbound.pop(0)
        if self._hangup is not None:
            await self._hangup.wait()
        raise ConnectionClosed

    def hang_up(self) -> None:
        """Ends a held-open connection, as a client closing its tab would."""
        if self._hangup is not None:
            self._hangup.set()

    async def close(self, *, code: int, reason: str) -> None:
        # Records rather than guarding against a second call: `close` never
        # raises, and a test asserting how a connection ended wants the
        # last word either way.
        self.closed_with = (code, reason)

    def types(self) -> list[MessageType]:
        """What the server sent, in order — the shape most assertions want."""
        return [message.type for message in self.sent]


class _Identity:
    """Structurally `auth`'s `RedeemedTicket` — the gateway depends on the
    shape rather than on `auth`'s domain module, so the fake satisfies
    `RedeemedIdentity` without importing it."""

    def __init__(self, *, player_id: UUID, session_id: UUID | None) -> None:
        self.player_id = player_id
        self.session_id = session_id


class FakeTicketRedeemer:
    """A `TicketRedeemer` that models **single use**.

    The one behaviour worth modelling rather than stubbing: a redemption
    removes the ticket, so a replay returns `None` from the same object that
    accepted the first presentation. A stub that always answered would leave
    the reuse path untested on the code that enforces it.
    """

    def __init__(self) -> None:
        self._tickets: dict[str, _Identity] = {}
        self.redemptions = 0

    def add(self, value: str, player_id: UUID, *, session_id: UUID | None = None) -> str:
        self._tickets[value] = _Identity(player_id=player_id, session_id=session_id)
        return value

    async def redeem(self, value: str) -> _Identity | None:
        self.redemptions += 1
        return self._tickets.pop(value, None)


class FakeConnectionRegistry:
    """The `gwconn:v1:` sorted set, as a dict of sets.

    No expiry modelling: every test here drives the lifecycle explicitly, and
    a fake clock in the registry would make "the entry lapsed" a property of
    the fake rather than of Redis. `refresh` reports presence or absence,
    which is the only thing the service branches on.
    """

    def __init__(self) -> None:
        self.connections: dict[UUID, dict[UUID, str]] = {}
        #: Makes every write raise, for the registration-failure path.
        self.fails = False
        self.unregister_calls: list[tuple[UUID, UUID]] = []

    async def register(
        self, player_id: UUID, connection_id: UUID, *, node_id: str, ttl_seconds: int
    ) -> int:
        if self.fails:
            raise RuntimeError("the registry is unreachable")
        # A dict keyed on the connection, so registering the same one twice
        # leaves one entry — which is what `ZADD` on an existing member does
        # — and so the node is recorded beside it, which is `gwconn:v2:`'s
        # whole point (A64-016.2 §2).
        self.connections.setdefault(player_id, {})[connection_id] = node_id
        return len(self.connections[player_id])

    async def unregister(self, player_id: UUID, connection_id: UUID) -> int:
        self.unregister_calls.append((player_id, connection_id))
        live = self.connections.setdefault(player_id, {})
        # `pop` with a default, not `del`: unregistering something already
        # gone is a no-op that still reports the true remaining count, which
        # is what makes the real adapter's cleanup idempotent.
        live.pop(connection_id, None)
        return len(live)

    async def refresh(
        self, player_id: UUID, connection_id: UUID, *, node_id: str, ttl_seconds: int
    ) -> bool:
        return connection_id in self.connections.get(player_id, {})

    async def active_count(self, player_id: UUID) -> int:
        return len(self.connections.get(player_id, {}))

    async def routes_for(self, player_id: UUID) -> Sequence[ConnectionRoute]:
        """Every connection and where it is — the read the router runs on.

        No expiry modelling, for the reason this class has never modelled
        one: every test here drives the lifecycle explicitly, and a fake
        clock in the registry would make "the entry lapsed" a property of
        the fake rather than of Redis.
        """
        return tuple(
            ConnectionRoute(
                player_id=player_id,
                connection_id=connection_id,
                node_id=node_id,
                expires_at=0.0,
            )
            for connection_id, node_id in self.connections.get(player_id, {}).items()
        )

    async def node_for(self, player_id: UUID, connection_id: UUID) -> str | None:
        return self.connections.get(player_id, {}).get(connection_id)


class RecordingPresence:
    """A `users.public.PresenceRecorder` that keeps every observation.

    Every call, in order, rather than the latest — "the player went offline
    once" and "the player went offline, then online, then offline" are
    different facts and the second is what a multi-connection bug looks
    like.
    """

    def __init__(self) -> None:
        self.observations: list[tuple[UUID, bool]] = []

    async def record_presence(
        self,
        player_id: UUID,
        *,
        is_online: bool,
        session_id: str | None = None,
        device_type: DeviceType | None = None,
    ) -> None:
        self.observations.append((player_id, is_online))

    def states_for(self, player_id: UUID) -> list[bool]:
        return [online for observed, online in self.observations if observed == player_id]


__all__ = [
    "StubMatchSnapshots",
    "InMemoryEventBuffer",
    "RecordingRemotePublisher",
    "InMemoryMoveIdempotency",
    "FakeSubmitMoves",
    "CountingMoveLimiter",
    "FakeConnectionRegistry",
    "FakeGatewaySocket",
    "FakeRoomMemberStore",
    "FakeTicketRedeemer",
    "RecordingPresence",
    "StubMatchRosters",
]


class FakeRoomMemberStore:
    """The `gwroom:v1:` sorted set and its reverse index, as two dicts.

    Models the two properties the room lifecycle actually rests on, and
    nothing else:

    **Membership is the `(player, connection)` pair.** So one tab leaving
    cannot take another out of the room — A64-016.2 §8's requirement, and
    the one a store keyed on the player alone would silently break.

    **Leave is idempotent.** Detaching an absent member removes nothing and
    still reports the truth, which is what the real `ZREM` plus read
    produces and what makes a disconnect racing an explicit `room.leave`
    safe.

    Not modelled: expiry, and the atomicity of "add then read the members".
    Both belong to Redis, and both are asserted against it in
    `tests/contract/test_gateway_redis.py` — the same line every fake on
    this platform draws.
    """

    def __init__(self) -> None:
        self.rooms: dict[UUID, list[RoomMember]] = {}
        self.rooms_of_connection: dict[UUID, set[UUID]] = {}
        self.progress: dict[UUID, RoomProgress] = {}

    async def join(
        self, match_id: UUID, member: RoomMember, *, ttl_seconds: int
    ) -> Sequence[RoomMember]:
        members = self.rooms.setdefault(match_id, [])
        if member not in members:
            members.append(member)
        self.rooms_of_connection.setdefault(member.connection_id, set()).add(match_id)
        return tuple(members)

    async def leave(self, match_id: UUID, member: RoomMember) -> Sequence[RoomMember]:
        members = self.rooms.setdefault(match_id, [])
        if member in members:
            members.remove(member)
        self.rooms_of_connection.get(member.connection_id, set()).discard(match_id)
        return tuple(members)

    async def members_of(self, match_id: UUID) -> Sequence[RoomMember]:
        return tuple(self.rooms.get(match_id, []))

    async def record_progress(
        self, match_id: UUID, *, ply: int, side_to_move: str, fingerprint: str
    ) -> bool:
        """Monotonic, like the real Lua: a write that would move the room
        backwards is refused. Modelled because an out-of-order delivery is
        ordinary under at-least-once fan-out, not a rare race."""
        current = self.progress.get(match_id)
        if current is not None and current.ply >= ply:
            return False

        self.progress[match_id] = RoomProgress(
            ply=ply, side_to_move=side_to_move, fingerprint=fingerprint
        )
        return True

    async def progress_of(self, match_id: UUID) -> RoomProgress | None:
        return self.progress.get(match_id)

    async def leave_all(self, member: RoomMember) -> Sequence[UUID]:
        left = tuple(self.rooms_of_connection.pop(member.connection_id, set()))
        for match_id in left:
            members = self.rooms.setdefault(match_id, [])
            if member in members:
                members.remove(member)
        return left


class StubMatchRosters:
    """A `game.public.MatchRosterReader` a test dictates the answers of.

    A stub rather than the real `GameMatchRoster` for the reason `game`'s
    match creation is stubbed in the pairing suite: what these tests are
    about is what the *gateway* does with an answer, not how `game` arrives
    at one. The read itself is a primary-key lookup and a projection, and
    exercising it here would make every room test also a database test.

    Returns `None` for an unknown match, which is the published contract —
    and the case §7's "non-participant cannot join" shares with a match that
    does not exist, deliberately.
    """

    def __init__(self) -> None:
        self.rosters: dict[UUID, MatchRoster] = {}

    def add(
        self,
        match_id: UUID,
        light: UUID,
        dark: UUID,
        *,
        status: MatchRecordStatus = MatchRecordStatus.ACTIVE,
    ) -> MatchRoster:
        roster = MatchRoster(
            match_id=match_id, light_player_id=light, dark_player_id=dark, status=status
        )
        self.rosters[match_id] = roster
        return roster

    async def roster_of(self, match_id: UUID) -> MatchRoster | None:
        return self.rosters.get(match_id)


class FakeSubmitMoves:
    """A `SubmitMoveUseCase` a test dictates the answers of.

    A stub rather than the real `LiveMoveService`, and the line is the one
    every gateway fake draws: what these tests are about is the *transport*
    — the ordering of the checks, the wire mapping, the fan-out — not
    whether the engine agrees a path is legal. That is
    `tests/unit/test_move_generation.py`'s, and §16 forbids duplicating it.

    `raises` is what makes the error-mapping assertions possible without
    constructing a position that produces each failure.
    """

    def __init__(self) -> None:
        self.raises: Exception | None = None
        self.submissions: list[SubmitMoveRequest] = []
        self._ply = 0

    async def submit(self, request: SubmitMoveRequest) -> SubmitMoveResult:
        self.submissions.append(request)
        if self.raises is not None:
            raise self.raises

        self._ply += 1
        return SubmitMoveResult(
            match_id=request.match_id,
            ply=self._ply,
            side_to_move=PlayerSide.DARK,
            fingerprint=f"fingerprint-{self._ply}",
            applied=AppliedMove(path=request.path, captured=(), promoted_to=None),
        )


class FakeGameCommands:
    """A `GameCommandUseCase` a test dictates the answers of.

    The same line every gateway fake draws: these tests are about the
    *transport* — which frame maps to which command, who is refused, who
    receives the fan-out — not about whether resigning settles a match,
    which is `tests/contract/test_game_commands.py`'s.

    `raises` is what makes the error-mapping assertions possible without
    constructing a match in each of eight refusable states.
    """

    def __init__(self) -> None:
        self.raises: Exception | None = None
        self.executed: list[GameCommandRequest] = []
        self.ply = 4

    async def execute(self, request: GameCommandRequest) -> GameCommandResult:
        self.executed.append(request)
        if self.raises is not None:
            raise self.raises

        terminal = request.command in (GameCommand.RESIGN, GameCommand.ACCEPT_DRAW)
        offering = request.command is GameCommand.OFFER_DRAW
        return GameCommandResult(
            match_id=request.match_id,
            command=request.command,
            acting_side=PlayerSide.LIGHT,
            acting_player_id=request.player_id,
            ply=self.ply,
            offer=(
                DrawOfferView(offered_by=PlayerSide.LIGHT, offered_at_ply=self.ply, offered_at=NOW)
                if offering
                else None
            ),
            outcome=(
                MatchOutcome.WIN
                if request.command is GameCommand.RESIGN
                else MatchOutcome.DRAW
                if request.command is GameCommand.ACCEPT_DRAW
                else None
            ),
            termination_reason=(
                TerminationReason.RESIGNATION
                if request.command is GameCommand.RESIGN
                else TerminationReason.AGREED_DRAW
                if request.command is GameCommand.ACCEPT_DRAW
                else None
            ),
            winner=PlayerSide.DARK if request.command is GameCommand.RESIGN else None,
            settled_at=NOW if terminal else None,
            # A64-020.5D. `is_settled` is false while an offer stands, so a
            # test asserting the participant frame gets one — and true
            # otherwise, so the frames are absent exactly where production
            # would omit them.
            draw=DrawAgreementView(
                offer=(
                    DrawOfferView(
                        offered_by=PlayerSide.LIGHT, offered_at_ply=self.ply, offered_at=NOW
                    )
                    if offering
                    else None
                ),
                may_offer_light=not offering,
                may_offer_dark=not offering,
                is_untouched=not offering,
            ),
        )


class InMemoryMoveIdempotency:
    """`MoveIdempotency` as a dict keyed on `(connection, request_id)`.

    The scope is the point, and it is modelled rather than simplified: a
    store keyed on the player would make one tab's retry return the other
    tab's answer, which is the bug §7's explicit scoping exists to prevent.

    No expiry. The TTL is Redis's and a fake clock here would make "the
    entry lapsed" a property of the fake.
    """

    def __init__(self) -> None:
        self.answers: dict[tuple[UUID, str], GatewayMessage] = {}

    async def replay(self, connection_id: UUID, request_id: str) -> GatewayMessage | None:
        return self.answers.get((connection_id, request_id))

    async def remember(
        self, connection_id: UUID, request_id: str, *, frame: GatewayMessage, ttl_seconds: int
    ) -> None:
        self.answers[(connection_id, request_id)] = frame


class CountingMoveLimiter:
    """A `MoveRateLimiter` that allows a fixed number and then refuses.

    Counts calls, so a test can assert that a refused submission happened
    **before** any expensive work — the property §13 asks for and the one
    a limiter placed after the game check would silently lose.
    """

    def __init__(self, allowance: int) -> None:
        self.allowance = allowance
        self.calls = 0

    async def allow(self, connection_id: UUID) -> bool:
        self.calls += 1
        if self.allowance <= 0:
            return False
        self.allowance -= 1
        return True


class RecordingRemotePublisher:
    """A `RemoteNodePublisher` that keeps every forwarding request.

    Keeps them rather than counting, because the property worth asserting
    is **one publish per node** — a publisher that sent one per connection
    would produce the same count when every player has one tab and the
    wrong one the moment somebody opens a second.
    """

    def __init__(self, *, succeeds: bool = True) -> None:
        self.published: list[ForwardingRequest] = []
        self._succeeds = succeeds

    async def publish(self, request: ForwardingRequest) -> bool:
        self.published.append(request)
        return self._succeeds


class InMemoryEventBuffer:
    """The `gwevent:v1:` sorted set, as a dict of lists.

    Models the two properties the reconnect decision rests on, and nothing
    else:

    **Idempotent on the sequence.** Appending the same ply twice leaves one
    entry, which is what `ZADD` on an existing member does and what makes an
    at-least-once fan-out safe to buffer.

    **Continuity is proven by the oldest entry**, not by the count — a buffer
    that trimmed past the client can still return frames, and returning them
    would be the silent partial recovery §6 forbids.

    Not modelled: the TTL and the rank trim's atomicity, both of which belong
    to Redis and are asserted against it in
    `tests/contract/test_state_sync.py`.
    """

    def __init__(self, *, max_events: int = 64) -> None:
        self.events: dict[UUID, dict[int, str]] = {}
        self._max_events = max_events

    async def append(self, match_id: UUID, *, sequence: int, frame: str) -> None:
        buffered = self.events.setdefault(match_id, {})
        buffered.setdefault(sequence, frame)

        for stale in sorted(buffered)[: max(0, len(buffered) - self._max_events)]:
            del buffered[stale]

    async def since(self, match_id: UUID, *, sequence: int) -> BufferedEvents:
        buffered = self.events.get(match_id, {})
        if not buffered:
            return BufferedEvents(frames=(), is_contiguous=False)

        return BufferedEvents(
            frames=tuple(buffered[key] for key in sorted(buffered) if key > sequence),
            is_contiguous=min(buffered) <= sequence + 1,
        )

    async def length(self, match_id: UUID) -> int:
        return len(self.events.get(match_id, {}))


class StubMatchSnapshots:
    """A `game.public.MatchSnapshotReader` a test dictates the answers of.

    A stub rather than the real `GameMatchSnapshot` for the reason every
    gateway fake is one: what these tests are about is what the *gateway*
    does with a snapshot — the resync decision, the membership check, the
    payload projection — not how `game` replays a log to build one, which is
    `tests/contract/test_move_log.py`'s.
    """

    def __init__(self) -> None:
        self.snapshots: dict[UUID, MatchSnapshot] = {}

    def add(
        self,
        match_id: UUID,
        *,
        light: UUID,
        dark: UUID,
        sequence: int = 0,
        clock: ClockView | None = None,
        status: MatchRecordStatus = MatchRecordStatus.ACTIVE,
        rated: bool = True,
        draw_offer: DrawOfferState | None = None,
        may_offer_light: bool = True,
        may_offer_dark: bool = True,
    ) -> MatchSnapshot:
        snapshot = MatchSnapshot(
            match_id=match_id,
            engine_version=2,
            variant=ProductVariant.RUSSIAN_8X8,
            status=status,
            rated=rated,
            sequence=sequence,
            side_to_move=PlayerSide.LIGHT if sequence % 2 == 0 else PlayerSide.DARK,
            fingerprint=f"fingerprint-{sequence}",
            pieces=(PlacedPiece(square="c3", side="light", rank="man"),),
            light_player_id=light,
            dark_player_id=dark,
            clock=clock,
            draw_offer=draw_offer,
            may_offer_light=may_offer_light,
            may_offer_dark=may_offer_dark,
            outcome=None,
            termination_reason=None,
            winner=None,
            observed_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        )
        self.snapshots[match_id] = snapshot
        return snapshot

    async def snapshot_of(self, match_id: UUID) -> MatchSnapshot | None:
        return self.snapshots.get(match_id)


class InMemorySpectatorStore:
    """The `gwspec:v1:` keyspace and its reverse index, as two dicts.

    Models what the fan-out and the disconnect path actually depend on:
    subscriptions are keyed on `(player, connection)` so a viewer with two
    tabs is two entries, and a connection can be removed from every match it
    watches without knowing which those were.

    Not modelled: the TTL and the score-range liveness filter, both of which
    belong to Redis and are asserted against it in
    `tests/contract/test_spectating.py`.
    """

    def __init__(self) -> None:
        self.watching: dict[UUID, set[SpectatorSubscription]] = {}
        self.fails = False

    async def subscribe(
        self, match_id: UUID, subscription: SpectatorSubscription, *, ttl_seconds: int
    ) -> int:
        audience = self.watching.setdefault(match_id, set())
        audience.add(subscription)
        return len(audience)

    async def unsubscribe(self, match_id: UUID, subscription: SpectatorSubscription) -> int:
        audience = self.watching.setdefault(match_id, set())
        audience.discard(subscription)
        return len(audience)

    async def routes_for(self, match_id: UUID) -> Sequence[SpectatorSubscription]:
        if self.fails:
            raise ConnectionError("spectator store unavailable")
        return tuple(sorted(self.watching.get(match_id, ()), key=lambda sub: sub.connection_id))

    async def unsubscribe_all(self, subscription: SpectatorSubscription) -> Sequence[UUID]:
        left = [
            match_id for match_id, audience in self.watching.items() if subscription in audience
        ]
        for match_id in left:
            self.watching[match_id].discard(subscription)
        return tuple(left)


class StubSpectatorPolicy:
    """A `SpectatorEligibility` a test dictates the answer of.

    A stub rather than `BlockAwareSpectatorPolicy`, because the *policy* has
    its own tests over a real block graph — what the handler tests are about
    is that a refusal becomes the right wire code and that an admission
    subscribes.
    """

    def __init__(self, refusal: SpectatorRefusal | None = None) -> None:
        self.refusal = refusal
        self.asked: list[UUID] = []

    async def refusal_for(
        self, snapshot: MatchSnapshot, *, player_id: UUID
    ) -> SpectatorRefusal | None:
        self.asked.append(player_id)
        return self.refusal


class StubSocialGraph:
    """A `friends.public.SocialGraphReader` a test dictates the answer of.

    Only `blocked_ids_for` is meaningful: it is the one method the
    quick-message path calls, and the set it returns is **symmetric** the
    way the real port's is — a test states "these two cannot interact"
    without saying which of them placed the block, because the production
    code cannot tell either.

    `fails` is what makes the fail-closed posture testable: a social graph
    that cannot be read must suppress rather than deliver, and a stub that
    could only succeed would leave that branch unexercised.
    """

    def __init__(self, *, blocked: dict[UUID, frozenset[UUID]] | None = None) -> None:
        self.blocked = blocked or {}
        self.fails = False
        self.reads: list[UUID] = []

    def block(self, one: UUID, other: UUID) -> None:
        """Records a block between two players, in both directions."""
        self.blocked[one] = self.blocked.get(one, frozenset()) | {other}
        self.blocked[other] = self.blocked.get(other, frozenset()) | {one}

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        return set()

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        self.reads.append(player_id)
        if self.fails:
            raise RuntimeError("the social graph is unreachable")
        return self.blocked.get(player_id, frozenset())


class StubPairingExclusions:
    """A `friends.public.PairingExclusions` over a set of blocked pairs.

    Symmetric, as the real one is: `blocked_pairs_among` reports the
    exclusion from both sides, which is what makes BL-1's invisibility hold.
    """

    def __init__(self, blocked: Sequence[tuple[UUID, UUID]] = ()) -> None:
        self.blocked = tuple(blocked)
        self.fails = False

    async def blocked_pairs_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        if self.fails:
            raise ConnectionError("block graph unavailable")

        named = set(player_ids)
        pairs: dict[UUID, set[UUID]] = {}
        for blocker, blocked in self.blocked:
            if blocker not in named or blocked not in named:
                continue
            pairs.setdefault(blocker, set()).add(blocked)
            pairs.setdefault(blocked, set()).add(blocker)

        return {player: frozenset(against) for player, against in pairs.items()}
