"""What the gateway holds — A64-016.1 §8.

Four protocols, none of which mentions FastAPI, Starlette or Redis. That is
the whole reason this module exists: `GatewayConnectionService` is where the
connection lifecycle lives, and a lifecycle that could only run inside a real
WebSocket would be a lifecycle nobody could test without one.

## Why the transport is a port and not a parameter

`GatewaySocket` is the interesting one. A route could hand the service a
Starlette `WebSocket` directly and the code would be shorter — and every
assertion about disconnect ordering, heartbeat timeouts and cleanup would
then need a real socket, an event loop, and a client driving it. Behind this
protocol the same assertions are a list of frames and a flag.

It is also the seam AD-11's multiplexing lands on: a channel-aware socket is
another implementation of these three methods, not a change to the service.

## Ports are declared here, by the layer that needs them — AD-06

`PresenceRecorder` is deliberately **not** redeclared: `users.public` already
publishes exactly the operation this needs, and its own docstring names this
task as its caller ("Its caller is AD-09's gateway. That is a wiring change
in the task that opens the sockets"). Redeclaring it would be a second
contract for one capability.

`TicketRedeemer` *is* declared here, because `auth.public` publishes
`AuthenticatedUser` and nothing else — that module states plainly that
nothing which can mint or inspect a credential is published, and every
consumer reaches authentication through `auth.presentation.dependencies`.
So the gateway names the shape it needs and the composition root supplies
`WebSocketTicketService`, which satisfies it structurally.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.gateway.protocol import GatewayMessage
from app.gateway.rooms import RoomMember, RoomProgress


@dataclass(frozen=True, slots=True)
class ConnectionRoute:
    """Where one live connection is — A64-016.2 §2.

    The unit `gwconn:v2:` exists to carry. A64-016.1's registry recorded
    *that* a player had a connection and not **where**, so nothing could
    route a message to the socket holding it; this is the missing half, and
    it is a value rather than a tuple so that a caller reads
    `route.node_id` instead of `route[1]`.

    Frozen: a route is an observation of the registry at one instant, and a
    caller that could edit one would be a caller whose routing plan drifts
    from what the registry says.
    """

    player_id: UUID
    connection_id: UUID
    node_id: str
    """Which gateway process holds the socket. Internal topology — never in
    a client payload and never in a metric label (§3, §11)."""

    expires_at: float
    """Epoch seconds. The registry's own TTL metadata, carried out so a
    caller can see how stale a route is without a second read — which is
    what turns "this node stopped refreshing" from invisible into
    diagnosable."""


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    """How a set of recipients divides between this node and the rest —
    A64-016.2 §9.

    Deliberately a **plan and not a delivery**. §9 is explicit that no
    broker and no remote delivery is built in this task, and the value of
    computing the split now is that A64-016.3 writes the transport against
    a shape that already exists and is already tested, rather than
    designing both at once.

    Two fields rather than one list with a flag, because the two halves are
    consumed by different code: the local half is a loop over sockets this
    process holds, and the remote half is a publish per node. A flag would
    make every consumer re-derive the partition.
    """

    local: Sequence[ConnectionRoute]
    """Connections this process holds. Deliverable now, by writing to the
    socket."""

    remote: Mapping[str, Sequence[ConnectionRoute]]
    """Connections held elsewhere, grouped by the node that holds them.

    Grouped rather than flat because the eventual transport is one message
    per *node*, not one per connection — a fan-out that published per
    recipient would send the same frame to one node as many times as it has
    that player's tabs open.
    """

    @property
    def is_fully_local(self) -> bool:
        """Whether everything can be delivered without leaving this process.

        The common case on a single-node deployment and during development,
        and the one an operator wants counted (§11) — a rising remote share
        is what says the fleet has actually started to need a transport.
        """
        return not self.remote


class RedeemedIdentity(Protocol):
    """What a spent ticket proves — structurally `auth`'s `RedeemedTicket`.

    A protocol rather than an import, so the gateway depends on the *shape*
    of a redemption rather than on `auth`'s domain module. Two attributes,
    both of which the presence record needs.
    """

    @property
    def player_id(self) -> UUID: ...

    @property
    def session_id(self) -> UUID | None: ...


class TicketRedeemer(Protocol):
    """Spends AD-09's ticket. One method, and deliberately only one.

    The gateway can redeem and cannot mint, which is not a convenience: a
    transport tier that could issue credentials would be a transport tier
    that could issue one for any account, and R-7's "the gateway contains no
    domain logic" is worth nothing if it can hand itself an identity.
    """

    async def redeem(self, value: str) -> RedeemedIdentity | None:
        """`None` for unknown, expired and already-spent alike — the caller
        closes the socket identically for all three."""
        ...


class GatewaySocket(Protocol):
    """One client connection, as the lifecycle sees it.

    Three methods, framework-free. Errors are modelled as exceptions rather
    than return values because a socket that has gone away cannot be
    reported to in a return value — every subsequent call would have to be
    checked, and the one that is forgotten is the one that hangs.
    """

    async def send(self, message: GatewayMessage) -> None:
        """Writes one frame.

        Raises `ConnectionClosed` if the peer has gone. It does **not**
        swallow that: a service that could not tell whether its frame
        arrived would keep a dead connection registered until the TTL
        expired, which is the failure the registry's own expiry exists as a
        backstop for rather than as a primary mechanism.
        """
        ...

    async def receive(self) -> str:
        """Waits for one frame and returns it undecoded.

        **A string, not a `GatewayMessage`.** Decoding is the protocol
        module's and its failure is a message the service sends back, so a
        transport that decoded would have to know about `error` frames —
        which is exactly the domain knowledge R-7 keeps out of the
        transport.

        Raises `ConnectionClosed` when the peer disconnects, which is the
        ordinary way a connection ends rather than an exceptional one.
        """
        ...

    async def close(self, *, code: int, reason: str) -> None:
        """Closes the connection. Never raises.

        Because it runs in cleanup, on every path including the one that is
        already handling a failure — and an exception there would replace
        the original error with a less useful one and skip the unregister
        that has to happen regardless.
        """
        ...


class ConnectionRegistry(Protocol):
    """Which connections a player has open, across every gateway node.

    Not per-process state, and that is the design: "does this player have
    another connection" has to be answered about the *fleet*, or a player
    with one tab on node A and one on node B goes offline whenever either
    closes.

    ## Why register and unregister return a count

    They return the number of live connections **after** the operation, and
    that is what makes the presence transition correct under concurrency.
    The alternative — write, then read the count — has a window between the
    two in which another node's connect or disconnect lands, so two closing
    sockets can both read zero and two opening ones can both read one.
    Returning the count from the same atomic operation removes the window
    entirely: exactly one caller ever sees `1` on the way up and exactly one
    ever sees `0` on the way down.
    """

    async def register(
        self, player_id: UUID, connection_id: UUID, *, node_id: str, ttl_seconds: int
    ) -> int:
        """Records an open connection. Returns the live count including it.

        A return of `1` means *this* connection is the player's first, and
        is the signal to mark them online. Idempotent on `connection_id`:
        registering the same one twice leaves one entry and a count that
        counts it once.

        `node_id` since A64-016.2 — the connection is recorded **with its
        location**, in the same write, because a registry that recorded
        membership first and location second would have a window in which a
        connection exists and cannot be routed to.
        """
        ...

    async def unregister(self, player_id: UUID, connection_id: UUID) -> int:
        """Forgets a connection. Returns the live count of what remains.

        A return of `0` means this was the player's last, and is the signal
        to mark them offline. **Idempotent**: unregistering a connection
        that is already gone removes nothing and returns the true remaining
        count — so a double cleanup cannot take a player offline while
        another connection is open, which is A64-016.1 §7's requirement.
        """
        ...

    async def refresh(
        self, player_id: UUID, connection_id: UUID, *, node_id: str, ttl_seconds: int
    ) -> bool:
        """Extends a connection's expiry. `False` if it was no longer there.

        The heartbeat's write. `False` means the entry lapsed — the node
        stopped refreshing long enough for another node to reap it — and the
        connection should be treated as gone rather than silently
        resurrected, because a resurrection would make the fleet's count
        disagree with itself.
        """
        ...

    async def active_count(self, player_id: UUID) -> int:
        """How many connections a player has open right now.

        A read, for operators and for tests. Nothing in the lifecycle calls
        it — see the note above on why the counts that decide presence come
        back from the writes instead.
        """
        ...

    async def routes_for(self, player_id: UUID) -> Sequence[ConnectionRoute]:
        """Every live connection this player has, and where each one is —
        A64-016.2 §2.

        The read the router is built on. Returns **routes rather than
        counts**, which is the whole difference between `gwconn:v1:` and
        `gwconn:v2:`: a count answers "is this player online" and a route
        answers "where do I send this".

        Empty for a player with no connections — an ordinary answer, not a
        failure. Excludes entries that have lapsed, so a node that died
        does not appear as a destination.
        """
        ...

    async def node_for(self, player_id: UUID, connection_id: UUID) -> str | None:
        """Which node holds one connection, or `None` if it is not live.

        §2 asks for this explicitly. Built on the same read as
        `routes_for` rather than on a second index keyed by connection, so
        there is one structure and nothing to keep in step — see
        `RedisConnectionRegistry` on why a second key would be the thing
        that drifts.
        """
        ...


class ConnectionRouter(Protocol):
    """Splits a set of recipients into what this node can deliver and what
    it cannot — A64-016.2 §9.

    A port with no transport behind it, and deliberately so: §9 asks for
    "the routing seam required by future tasks" and forbids the broker. The
    seam is worth having now because it is where the *decision* lives, and
    a decision that is already a tested value is one A64-016.3 can write a
    transport against instead of inventing both together.
    """

    async def plan_for(self, player_ids: Sequence[UUID]) -> RoutingPlan:
        """Where every live connection of every named player is.

        Batched, and that is not an optimisation: the caller is a fan-out
        to a room's participants, and a plan built one player at a time
        would be the N+1 the batch exists to avoid on the hottest path the
        gateway will ever have.
        """
        ...


class RoomMemberStore(Protocol):
    """Which connections are attached to a match — A64-016.2 §6.

    Storage only. Whether a socket *may* attach is `GameRoomService`'s
    question, answered against `game.public`, and a store that could refuse
    would be a second place holding the membership rule — which is how one
    of the two ends up enforcing a stale version of it.

    **No `create` and no `delete`.** A room is not opened and not closed: it
    is the set of members for a match id, and it exists exactly as long as
    that set is non-empty (§8 — "empty room expires after TTL"). An explicit
    lifecycle would be a state machine with nothing to observe it.
    """

    async def join(
        self, match_id: UUID, member: RoomMember, *, ttl_seconds: int
    ) -> Sequence[RoomMember]:
        """Attaches one connection. Returns the room's members afterwards.

        The members come back **from the same operation**, for the reason
        `ConnectionRegistry.register` returns a count: "are both players
        here now" is a question about the state this write produced, and a
        separate read has a window that the other player's join lands in.

        Idempotent on `(player_id, connection_id)` — a client that retries
        a join after a dropped response is attached once.
        """
        ...

    async def leave(self, match_id: UUID, member: RoomMember) -> Sequence[RoomMember]:
        """Detaches one connection. Returns what remains.

        **Idempotent** (§8): detaching a connection that is not attached
        removes nothing and reports the truth, so a disconnect cleanup that
        runs after an explicit `room.leave` cannot take the player's other
        tabs out of the room.
        """
        ...

    async def members_of(self, match_id: UUID) -> Sequence[RoomMember]:
        """Everything currently attached. Empty for a room nobody is in —
        which is the same answer as for a match that does not exist, because
        a store has no way to tell and no business telling."""
        ...

    async def record_progress(
        self, match_id: UUID, *, ply: int, side_to_move: str, fingerprint: str
    ) -> bool:
        """Records the room's live projection — A64-016.3 §11.

        The minimum a client needs to synchronise and a router needs to
        order: a sequence, whose turn it is, and a fingerprint. **Not the
        board.** `game` is authoritative (AD-18) and this is an ephemeral
        read projection; duplicating the aggregate here would be a second
        thing to be wrong.

        **Monotonic** — returns `False` and writes nothing when `ply` is not
        greater than what is stored. §12 asks for exactly this: "concurrent
        updates cannot silently overwrite a newer sequence". Two moves
        delivered out of order would otherwise leave the room reporting the
        earlier one, and a client resynchronising against it would be sent
        backwards.

        `False` is not an error. It means a newer move already landed,
        which under at-least-once delivery is an ordinary race, and the
        caller has nothing to do about it.
        """
        ...

    async def progress_of(self, match_id: UUID) -> RoomProgress | None:
        """The room's live projection, or `None` if no move has landed."""
        ...

    async def leave_all(self, member: RoomMember) -> Sequence[UUID]:
        """Detaches one connection from every room it is in. Returns which.

        The disconnect path. A connection that dropped has no chance to send
        `room.leave`, and without this its member would sit in the room
        until the TTL — during which `both_connected` reports a player who
        is not there, which is the one thing the room exists to answer.
        """
        ...


class LocalSocketRegistry(Protocol):
    """The sockets **this process** holds, by connection id — A64-016.3 §10.

    Process-local, and that is not a violation of "no process-local state
    for correctness". A socket is a file descriptor owned by one process:
    no other node can write to it, so a map from connection id to socket is
    a fact about this process rather than a cached opinion about the fleet.
    The fleet-wide question — *which* node holds a connection — is
    `ConnectionRegistry`'s, and that one is in Redis precisely because it is
    shared.

    The two are used together: `RoutingPlan` says a route is local, and this
    turns that route into something writable.
    """

    def attach(self, connection_id: UUID, socket: GatewaySocket) -> None:
        """Records a socket this process is serving."""
        ...

    def detach(self, connection_id: UUID) -> None:
        """Forgets one. Idempotent — detaching an absent connection is a
        no-op, so a cleanup that runs twice cannot remove a reconnection
        that happened to reuse the id."""
        ...

    def socket_for(self, connection_id: UUID) -> GatewaySocket | None:
        """The socket, or `None` if this process is not holding it.

        `None` is an **ordinary** answer, not a failure: a connection can
        close between a routing plan being built and a frame being written,
        and §10 requires that to be tolerated rather than to fail the move.
        """
        ...


@dataclass(frozen=True, slots=True)
class ForwardingRequest:
    """One node's share of a fan-out — A64-016.3 §9.

    **Primitive-only.** No `GatewaySocket`, no FastAPI `WebSocket`, no
    engine type: §9 requires a "stable primitive payload", and the reason is
    that the eventual transport serialises this onto a bus. A value that
    could hold a socket is a value that cannot leave the process, which
    would make the seam a lie.

    One of these per **node**, never per connection — see
    `RemoteNodePublisher`.
    """

    node_id: str
    connection_ids: tuple[UUID, ...]
    """Which of that node's connections should receive the frame.

    Carried rather than left for the receiving node to recompute, because
    the routing plan is the source of truth (§8) and a receiver that
    re-derived the recipient set could disagree with the sender about who
    was in the room a moment ago.
    """

    frame: str
    """The encoded envelope, exactly as it would be written to a socket.

    A string rather than a `GatewayMessage`, so the receiving node writes it
    without re-encoding — and so that a frame cannot mean two things because
    two builds serialised the same message differently.
    """


class RemoteNodePublisher(Protocol):
    """Hands one node's share of a fan-out to whatever will carry it —
    A64-016.3 §9.

    **The seam, not the transport.** §9 is explicit that this task does not
    build a broker, does not use Pub/Sub and takes no Celery dependency; it
    asks for the abstraction the next task implements behind.

    The contract that matters for that implementation:

    **Never raises.** A move is committed before anything is delivered
    (§9 — "failures do not roll back an already accepted move"), so a
    publisher that raised would turn a delivery problem into a move that
    appears to have failed after it was applied. It reports success and the
    caller records the failure.

    **Safely repeatable.** The frame carries a ply, and a client that
    receives the same ply twice ignores the second — so at-least-once
    delivery is acceptable and the eventual transport need not deduplicate.
    """

    async def publish(self, request: ForwardingRequest) -> bool:
        """Hands off one node's share. `False` if it could not be handed
        off.

        Returns rather than raises — see this protocol's docstring. `False`
        is what the caller counts as a remote delivery failure, which is the
        only way an operator can see that half a fan-out is not arriving.
        """
        ...


class MoveIdempotency(Protocol):
    """Remembers one answer per `(connection, request_id)` — A64-016.3 §7.

    Deliberately typed in **messages** rather than strings, so the handler
    replays a frame it can hand straight to a socket and never re-renders a
    stored decision — two answers to one request that were rendered twice
    are two answers that can differ.
    """

    async def replay(self, connection_id: UUID, request_id: str) -> GatewayMessage | None:
        """The answer this request already produced, or `None`.

        `None` for a request never seen **and** for one whose entry has
        lapsed, indistinguishably — both mean "process it", and the ply
        compare-and-set is what stops a lapsed retry applying twice.
        """
        ...

    async def remember(
        self, connection_id: UUID, request_id: str, *, frame: GatewayMessage, ttl_seconds: int
    ) -> None:
        """Records the answer. **Never raises** — it runs after the move is
        already applied and acknowledged, so a failure costs a retry being
        reprocessed rather than anything a player can see."""
        ...


class MoveRateLimiter(Protocol):
    """Bounds how fast one connection may submit moves — A64-016.3 §13.

    Per **connection**, not per player: a player with two tabs is two
    clients, and a limit shared between them would let one tab's
    misbehaviour throttle the other's game.

    Its own port rather than the platform `RateLimiter` directly, because
    the answer this caller needs is a boolean — a socket has no headers to
    carry `Retry-After` into, so the decision object every HTTP route reads
    is machinery a handler would have to unpack and discard.
    """

    async def allow(self, connection_id: UUID) -> bool:
        """Whether this connection may submit now.

        **Never raises.** A limiter that failed would fail a move, and the
        platform's own posture is that a rate limit outage degrades rather
        than propagates — see `RateLimitSettings.fail_open`.
        """
        ...


class ConnectionClosed(Exception):
    """The peer has gone.

    Raised by `send` and `receive`. Deliberately **not** an error: it is how
    a connection normally ends, and the lifecycle treats it as the exit
    condition of its read loop rather than as a failure to report.
    """


__all__ = [
    "ConnectionClosed",
    "ConnectionRegistry",
    "ConnectionRoute",
    "ConnectionRouter",
    "ForwardingRequest",
    "GatewaySocket",
    "LocalSocketRegistry",
    "MoveIdempotency",
    "MoveRateLimiter",
    "RedeemedIdentity",
    "RemoteNodePublisher",
    "RoomMemberStore",
    "RoutingPlan",
    "TicketRedeemer",
]
