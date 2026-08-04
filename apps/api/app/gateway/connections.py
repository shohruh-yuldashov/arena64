"""`GatewayConnectionService` — the whole connection lifecycle. A64-016.1.

Redeem, register, announce, serve, clean up. Every rule in the task lives in
this one class, and nothing in it knows what a WebSocket is: the transport is
`GatewaySocket`, the store is `ConnectionRegistry`, presence is
`users.public.PresenceRecorder`, and the ticket is `TicketRedeemer`.

## The shape, and why cleanup is a `finally` around everything

    redeem  ->  register  ->  ready  ->  [ ping/pong ]*  ->  unregister

A64-016.1 §7 requires cleanup for a normal disconnect, an authentication
failure *after* the connection began, a malformed message failure, a
server-side exception and a heartbeat timeout. Those are five paths, and the
only structure that covers five paths without listing them is a `try/finally`
placed so that nothing between the register and the return can escape it.

The register is therefore the **first** statement inside the `try`, and it is
deliberately not before it: a connection that was never registered must not
be unregistered, because `unregister` on an absent entry returns the player's
true remaining count and would mark them offline while another tab is open.
That is the precise failure §7's last line names.

## Presence transitions come from the registry's return value

`register` returns the live count including this connection and `unregister`
returns what remains, both from one atomic operation. So:

    register returned 1    ->  this is the first connection   ->  online
    unregister returned 0  ->  this was the last connection   ->  offline

The alternative — write, then read the count — has a window between the two
that another node's connect or disconnect lands in, and two closing sockets
can both read zero. Reading the count *from* the write is what makes multiple
tabs correct across a fleet rather than only on one process.

## The client drives the heartbeat, and the server enforces a deadline

A server that pinged would need a timer per socket — 40,000 timers on a
gateway node, each waking to discover nothing has changed. Instead the client
pings and the server's read is `wait_for(receive(), timeout)`: an idle
connection costs one pending future, and the timeout is the enforcement.

A `ping` refreshes two things with different windows, and both are needed.
The registry entry has `GATEWAY_CONNECTION_TTL_SECONDS`, which is what makes
a crashed node's connections lapse; the presence record has
`PRESENCE_TTL_SECONDS`, which is what makes the *player* lapse. Refreshing
one and not the other produces either a player reported offline while holding
a socket, or a socket the fleet has forgotten while the player still shows
online.

## Failure posture

A malformed frame is answered with an `error` and the connection **stays
open**: a client that sends one bad frame is far more likely to be a version
skew or a bug than an attack, and closing would turn a recoverable client
defect into a reconnect loop against the whole fleet.

A presence write that fails is ignored — `PresenceRecorder` never raises, and
its own contract says a lost write costs a player looking offline until the
next heartbeat. A *registry* write that fails does close the connection,
because an unregistered connection is one nothing can route to.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from app.core.clock import Clock
from app.gateway.dispatch import MessageDispatch
from app.gateway.metrics import (
    CONNECTION_DURATION,
    CONNECTIONS_ACCEPTED,
    CONNECTIONS_CLOSED,
    CONNECTIONS_REJECTED,
    CloseReason,
    RejectionReason,
    RoomRejectionReason,
)
from app.gateway.moves import MoveSubmissionHandler
from app.gateway.ports import (
    ConnectionClosed,
    ConnectionRegistry,
    GatewaySocket,
    LocalSocketRegistry,
    TicketRedeemer,
)
from app.gateway.protocol import (
    GatewayErrorCode,
    GatewayMessage,
    MalformedFrame,
    MessageType,
    connection_ready,
    decode,
    error,
    pong,
    room_joined,
    room_left,
)
from app.gateway.resume import ResumeHandler
from app.gateway.room_service import GameRoomService, RoomJoinRefused
from app.gateway.spectator_handler import SpectatorHandler
from app.modules.users.public import DeviceType, PresenceRecorder
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)

#: RFC 6455 close codes, named so the call sites read as intent.
#:
#: `1008` (policy violation) for a refused ticket rather than `1011`
#: (internal error): the client did something the server declined, and a
#: client that retried an internal error would be right to, while one that
#: retried a spent ticket would loop forever.
CLOSE_POLICY_VIOLATION = 1008
CLOSE_NORMAL = 1000
CLOSE_INTERNAL_ERROR = 1011


@dataclass(frozen=True, slots=True)
class GatewayPolicy:
    """The three numbers the lifecycle reads, resolved once at composition.

    A value rather than four constructor arguments, so "what bounds this
    connection" is one object a test can vary and a reader can find. Their
    ordering invariant is checked in `GatewaySettings`, not here — a service
    that revalidated configuration would be a second place that has an
    opinion about it.
    """

    connection_ttl_seconds: int
    heartbeat_timeout_seconds: float
    max_frame_bytes: int

    node_id: str
    """Which gateway process this is — A64-016.2 §3.

    On the policy rather than resolved per connection, which is §3's
    "stable for the process lifetime" expressed where it cannot be got
    wrong: the lifecycle has no way to mint one, so every connection this
    process accepts is registered under the same node."""


class GatewayConnectionService:
    """One authenticated connection, from handshake to cleanup.

    Constructed per connection is *not* required — it holds no per-socket
    state, and `run` takes everything it needs. That is deliberate: a
    service with connection state would need one instance per socket, and
    40,000 instances holding four collaborators each is memory spent to
    express something an argument already says.
    """

    def __init__(
        self,
        *,
        tickets: TicketRedeemer,
        registry: ConnectionRegistry,
        rooms: GameRoomService,
        moves: MoveSubmissionHandler,
        resumes: ResumeHandler,
        spectators: SpectatorHandler,
        sockets: LocalSocketRegistry,
        presence: PresenceRecorder,
        metrics: MetricsRecorder,
        clock: Clock,
        policy: GatewayPolicy,
    ) -> None:
        self._tickets = tickets
        self._registry = registry
        self._rooms = rooms
        self._moves = moves
        self._resumes = resumes
        self._spectators = spectators
        self._sockets = sockets
        self._presence = presence
        self._metrics = metrics
        self._clock = clock
        self._policy = policy

    async def run(self, socket: GatewaySocket, *, ticket: str) -> None:
        """Serves one connection to completion. Never raises.

        The transport calls this and does nothing else, which is what keeps
        the route a thin adapter (§8). Every exit — refusal, disconnect,
        timeout, exception — leaves the registry and presence consistent by
        the time this returns.
        """
        identity = await self._tickets.redeem(ticket)
        if identity is None:
            # Before any registration, so there is nothing to clean up. The
            # code is the coarse one on purpose: unknown, expired and spent
            # are one answer (see `GatewayErrorCode.INVALID_TICKET`).
            await self._refuse(socket, RejectionReason.INVALID_TICKET)
            return

        connection_id = uuid4()
        try:
            live = await self._registry.register(
                identity.player_id,
                connection_id,
                node_id=self._policy.node_id,
                ttl_seconds=self._policy.connection_ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — a failed claim must close, not crash
            logger.error(
                "gateway_registration_failed",
                extra={"user_id": str(identity.player_id), "error": type(exc).__name__},
                exc_info=exc,
            )
            await self._refuse(socket, RejectionReason.REGISTRATION_FAILED)
            return

        await self._serve(
            socket, player_id=identity.player_id, connection_id=connection_id, live=live
        )

    async def _serve(
        self, socket: GatewaySocket, *, player_id: UUID, connection_id: UUID, live: int
    ) -> None:
        """Everything after a successful registration.

        Split from `run` purely so the `finally` cannot accidentally cover
        the pre-registration paths — the one arrangement that would
        reintroduce §7's "a broken socket marks a player offline while
        another remains active".
        """
        opened_at = self._clock.now()
        self._metrics.increment(CONNECTIONS_ACCEPTED)

        # Inside `_serve`, so it is covered by the same `finally` that
        # unregisters — a socket left in the local map after its connection
        # closed is a fan-out writing to a dead descriptor on every move.
        self._sockets.attach(connection_id, socket)

        # `1` means this connection is the player's first, from the same
        # atomic operation that made it true. See this module's docstring.
        if live == 1:
            await self._mark_online(player_id)

        reason = CloseReason.CLIENT
        try:
            await socket.send(connection_ready())
            logger.info(
                "gateway_connection_accepted",
                extra={"user_id": str(player_id), "live_connections": live},
            )
            reason = await self._read_loop(socket, player_id=player_id, connection_id=connection_id)
        except ConnectionClosed:
            # The peer went away mid-handshake — between the register and
            # the `connection.ready`. Ordinary, not an error.
            reason = CloseReason.CLIENT
        except Exception as exc:  # noqa: BLE001 — one socket must not take the node down
            reason = CloseReason.SERVER_ERROR
            logger.error(
                "gateway_connection_failed",
                extra={"user_id": str(player_id), "error": type(exc).__name__},
                exc_info=exc,
            )
        finally:
            await self._close(
                socket,
                player_id=player_id,
                connection_id=connection_id,
                reason=reason,
                opened_at=opened_at,
            )

    async def _read_loop(
        self, socket: GatewaySocket, *, player_id: UUID, connection_id: UUID
    ) -> CloseReason:
        """Reads frames until the peer goes or the deadline lapses.

        Returns how it ended rather than raising, because "the client left"
        and "the client stopped talking" are both normal terminations that
        differ only in what the operator should conclude.
        """
        while True:
            try:
                raw = await asyncio.wait_for(
                    socket.receive(), timeout=self._policy.heartbeat_timeout_seconds
                )
            except TimeoutError:
                # No player id in the metric label (§9); it is in the log,
                # which has the retention and access controls for it.
                logger.info("gateway_heartbeat_timeout", extra={"user_id": str(player_id)})
                return CloseReason.HEARTBEAT_TIMEOUT
            except ConnectionClosed:
                return CloseReason.CLIENT

            # **Here**, and nowhere later. MT-9 makes the gateway receive
            # instant the temporal authority for the flag race, and every
            # step between this line and the clock — dispatch, the room
            # read, the row lock, the engine — is platform delay a player
            # must not be charged for (A64-016.5 §2).
            received_at = self._clock.now()

            if not await self._handle(
                raw,
                socket,
                player_id=player_id,
                connection_id=connection_id,
                received_at=received_at,
            ):
                return CloseReason.CLIENT

    async def _handle(
        self,
        raw: str,
        socket: GatewaySocket,
        *,
        player_id: UUID,
        connection_id: UUID,
        received_at: datetime,
    ) -> bool:
        """One frame. `False` when the connection should stop.

        **A dispatch table since A64-016.3 §1.** The previous four-branch
        match reached its limit when a handler needed six collaborators of
        its own: every branch either forwarded a frame or was a two-line
        answer, and the move handler is neither.

        What is *not* a table is the decode and the send. Decoding fails
        before there is a type to dispatch on, and sending is the read
        loop's so that one place owns the socket — which is what makes
        AD-11's cross-stream ordering a property of the loop rather than of
        every handler behaving.
        """
        try:
            message = decode(raw, max_bytes=self._policy.max_frame_bytes)
        except MalformedFrame as malformed:
            # The connection stays open — see this module's docstring. The
            # detail goes to the log and the code goes to the client; the
            # payload itself is never logged (§9).
            logger.debug(
                "gateway_frame_rejected",
                extra={"user_id": str(player_id), "detail": malformed.detail},
            )
            return await self._try_send(socket, error(malformed.code))

        answer = await self._dispatch_for(
            socket,
            player_id=player_id,
            connection_id=connection_id,
            received_at=received_at,
        ).dispatch(message, player_id=player_id)

        if answer is None:
            return True
        return await self._try_send(socket, answer)

    def _dispatch_for(
        self,
        socket: GatewaySocket,
        *,
        player_id: UUID,
        connection_id: UUID,
        received_at: datetime,
    ) -> MessageDispatch:
        """This connection's table, bound to its identity.

        Built per frame rather than per connection, which is a dict of seven
        closures and is the cheaper of the two arrangements to reason about:
        the lifecycle holds no per-connection object, so there is nothing to
        tear down and nothing that can outlive the socket it closed over.

        Every entry is a bound method, so `CLIENT_SENDABLE` and this literal
        are the whole of the routing decision — there is no registry, no
        decorator and nothing discovered at import time (§1).
        """
        return MessageDispatch(
            {
                MessageType.PING: lambda message: self._answer_ping(
                    message, player_id=player_id, connection_id=connection_id
                ),
                MessageType.ROOM_JOIN: lambda message: self._join_room(
                    message, player_id=player_id, connection_id=connection_id
                ),
                MessageType.ROOM_LEAVE: lambda message: self._leave_room(
                    message, player_id=player_id, connection_id=connection_id
                ),
                MessageType.RESUME: lambda message: self._resumes.handle(
                    message, player_id=player_id, connection_id=connection_id
                ),
                MessageType.MOVE_SUBMIT: lambda message: self._moves.handle(
                    message,
                    player_id=player_id,
                    connection_id=connection_id,
                    received_at=received_at,
                ),
                MessageType.SPECTATOR_JOIN: lambda message: self._spectators.join(
                    message, player_id=player_id, connection_id=connection_id
                ),
                MessageType.SPECTATOR_LEAVE: lambda message: self._spectators.leave(
                    message, player_id=player_id, connection_id=connection_id
                ),
            }
        )

    async def _answer_ping(
        self, message: GatewayMessage, *, player_id: UUID, connection_id: UUID
    ) -> GatewayMessage:
        """`ping` — refreshes both liveness windows and answers.

        **Not charged against the move limit** (§13). A heartbeat is what
        keeps a connection alive; rate-limiting it would disconnect the
        players who are behaving.
        """
        await self._heartbeat(player_id=player_id, connection_id=connection_id)
        return pong(request_id=message.request_id)

    async def _join_room(
        self, message: GatewayMessage, *, player_id: UUID, connection_id: UUID
    ) -> GatewayMessage:
        """`room.join` — attaches this connection to a match's routing scope.

        **The player is `player_id`, the socket's proven identity**, never
        anything from the payload. §7 requires it and the frame makes it
        structural: `room.join` carries a match id and has no field a client
        could put a player in.
        """
        match_id = _match_id_of(message)
        if match_id is None:
            return error(
                GatewayErrorCode.MALFORMED_MESSAGE,
                request_id=message.request_id,
                channel=message.channel,
            )

        try:
            room = await self._rooms.join(
                match_id, player_id=player_id, connection_id=connection_id
            )
        except RoomJoinRefused as refused:
            return error(
                _REFUSAL_CODES[refused.reason],
                request_id=message.request_id,
                channel=message.channel,
            )

        return room_joined(
            match_id=room.match_id,
            participants=room.participants,
            both_connected=room.both_connected,
            request_id=message.request_id,
        )

    async def _leave_room(
        self, message: GatewayMessage, *, player_id: UUID, connection_id: UUID
    ) -> GatewayMessage:
        """`room.leave` — detaches this connection.

        Answered with `room.left` whether or not the connection was in the
        room, because §8 makes a repeated leave idempotent and a client that
        asked to be out and is out has got what it asked for. An error there
        would make a retry after a dropped response look like a failure.
        """
        match_id = _match_id_of(message)
        if match_id is None:
            return error(
                GatewayErrorCode.MALFORMED_MESSAGE,
                request_id=message.request_id,
                channel=message.channel,
            )

        await self._rooms.leave(match_id, player_id=player_id, connection_id=connection_id)
        return room_left(match_id=match_id, request_id=message.request_id)

    async def _heartbeat(self, *, player_id: UUID, connection_id: UUID) -> None:
        """Refreshes both windows a live connection depends on.

        The registry entry and the presence record have different TTLs and
        different owners, and refreshing only one produces a fleet that
        disagrees with itself — see this module's docstring.

        A registry entry that has already lapsed is re-registered rather
        than treated as fatal: the socket is demonstrably alive (a frame
        just arrived on it), so the entry lapsing means this node was slow
        rather than that the connection is gone, and dropping a working
        connection over bookkeeping would be the worse outcome.
        """
        refreshed = await self._registry.refresh(
            player_id,
            connection_id,
            node_id=self._policy.node_id,
            ttl_seconds=self._policy.connection_ttl_seconds,
        )
        if not refreshed:
            logger.warning("gateway_connection_lapsed", extra={"user_id": str(player_id)})
            await self._registry.register(
                player_id,
                connection_id,
                node_id=self._policy.node_id,
                ttl_seconds=self._policy.connection_ttl_seconds,
            )

        await self._mark_online(player_id)

    async def _close(
        self,
        socket: GatewaySocket,
        *,
        player_id: UUID,
        connection_id: UUID,
        reason: CloseReason,
        opened_at: datetime,
    ) -> None:
        """Cleanup. Runs exactly once per registered connection.

        "Exactly once" is structural rather than guarded by a flag: this is
        the only caller of `unregister`, it is reached only from one
        `finally`, and that `finally` covers a block that is entered once.
        A flag would be belt and braces over an invariant the control flow
        already has — and a flag on a per-connection object is state that
        has to be right, which is what this arrangement avoids having.

        Idempotent at the store as well, so a redelivery of this path from
        anywhere would still be safe: `unregister` on an absent entry
        removes nothing and returns the true remaining count.
        """
        # Before the unregister, so a connection is out of every room while
        # it is still a known connection. The other order would leave a
        # window in which the fleet has forgotten the socket and a room
        # still reports it as attached — which is the one thing
        # `both_connected` must never do.
        # Before both, so a fan-out that is already in flight finds no
        # socket rather than a closing one, and the room stops naming this
        # connection as a recipient.
        self._sockets.detach(connection_id)
        await self._rooms.detach(player_id=player_id, connection_id=connection_id)
        # And out of every audience. A64-016.7 §3: a socket that dropped
        # never sends `spectator.leave`, and a subscription left behind is a
        # fan-out to a dead descriptor on every move of a game nobody is
        # watching. `detach` never raises — see `SpectatorHandler`.
        await self._spectators.detach(player_id=player_id, connection_id=connection_id)

        remaining = 0
        try:
            remaining = await self._registry.unregister(player_id, connection_id)
        except Exception as exc:  # noqa: BLE001 — cleanup must not raise
            # The entry lapses on its own TTL, which is the backstop this
            # whole design has for a node that dies. Logged at `ERROR`
            # because a rising rate means presence is drifting.
            logger.error(
                "gateway_unregister_failed",
                extra={"user_id": str(player_id), "error": type(exc).__name__},
                exc_info=exc,
            )
            remaining = -1

        # `0` means this was the player's last connection. `-1` is the
        # unregister failing, where marking offline would be a guess — the
        # TTL settles it instead.
        if remaining == 0:
            await self._presence.record_presence(player_id, is_online=False)

        self._metrics.increment(CONNECTIONS_CLOSED, labels={"reason": reason})
        self._metrics.observe(CONNECTION_DURATION, (self._clock.now() - opened_at).total_seconds())
        logger.info(
            "gateway_connection_closed",
            extra={
                "user_id": str(player_id),
                "reason": reason.value,
                "live_connections": max(remaining, 0),
            },
        )

        await socket.close(
            code=CLOSE_INTERNAL_ERROR if reason is CloseReason.SERVER_ERROR else CLOSE_NORMAL,
            reason=reason.value,
        )

    async def _mark_online(self, player_id: UUID) -> None:
        """Records presence for a connected player.

        `DeviceType.WEB` unconditionally, and that is honest rather than
        lazy: the only client is a browser, nothing on the handshake carries
        a device claim, and a value inferred from a `User-Agent` string
        would be a guess stored in a keyspace whose docstring warns that a
        field added later decodes short on every key written before it.
        A64-016.2's mobile client supplies it properly, through the ticket.
        """
        await self._presence.record_presence(player_id, is_online=True, device_type=DeviceType.WEB)

    async def _refuse(self, socket: GatewaySocket, reason: RejectionReason) -> None:
        """Closes a handshake that never became a connection.

        The client gets a **different code and a different close code** for
        the two reasons, and the distinction is the client's retry policy
        rather than a diagnostic: a refused ticket must be replaced before
        reconnecting, and a registry that was briefly unreachable must be
        retried with a *fresh* ticket, because the one just presented has
        already been spent. Telling a client `invalid_ticket` when the
        ticket was fine would send it to re-authenticate; telling it
        `internal_error` when the ticket was replayed would make it loop.

        This is not the disclosure `GatewayErrorCode.INVALID_TICKET`
        refuses to make. That coarseness is about not distinguishing
        *unknown, expired and spent* — three ways for a client's own
        credential to be bad. This distinguishes the client's problem from
        the server's, which the client can already infer from the close
        code and which it needs in order to behave correctly.

        No `user_id` in the log line: for the common case there is no proven
        identity to name, since the ticket is exactly what failed.
        """
        self._metrics.increment(CONNECTIONS_REJECTED, labels={"reason": reason})
        logger.info("gateway_connection_rejected", extra={"reason": reason.value})

        if reason is RejectionReason.REGISTRATION_FAILED:
            await self._try_send(socket, error(GatewayErrorCode.INTERNAL_ERROR))
            await socket.close(code=CLOSE_INTERNAL_ERROR, reason=reason.value)
            return

        await self._try_send(socket, error(GatewayErrorCode.INVALID_TICKET))
        await socket.close(code=CLOSE_POLICY_VIOLATION, reason=reason.value)

    @staticmethod
    async def _try_send(socket: GatewaySocket, message: GatewayMessage) -> bool:
        """Sends, reporting whether the peer is still there.

        A refusal frame is a courtesy to a client that is about to be
        disconnected, and a peer that has already gone is the common case on
        that path — so the disconnect is the answer rather than an
        exception to handle at four call sites.
        """
        try:
            await socket.send(message)
        except ConnectionClosed:
            return False
        return True


#: Which wire code each refusal becomes. A mapping rather than a branch, so
#: a reason added to `RoomRejectionReason` without a code fails at import
#: rather than silently reaching a client as an internal error.
_REFUSAL_CODES: Final[dict[RoomRejectionReason, GatewayErrorCode]] = {
    RoomRejectionReason.NOT_A_PARTICIPANT: GatewayErrorCode.NOT_A_PARTICIPANT,
    RoomRejectionReason.ROOM_UNAVAILABLE: GatewayErrorCode.ROOM_UNAVAILABLE,
}


def _match_id_of(message: GatewayMessage) -> UUID | None:
    """The match a room frame names, or `None` if it named none usably.

    Validated here rather than in `decode`, because the envelope decoder
    knows nothing about what any particular payload should contain — and
    making it know would be the beginning of a schema registry inside the
    codec. `None` becomes a `malformed_message`, which is the same answer
    the decoder gives for every other unusable frame.
    """
    raw = message.payload.get("match_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


__all__ = [
    "CLOSE_INTERNAL_ERROR",
    "CLOSE_NORMAL",
    "CLOSE_POLICY_VIOLATION",
    "GatewayConnectionService",
    "GatewayPolicy",
]
