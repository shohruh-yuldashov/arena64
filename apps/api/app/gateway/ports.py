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

from typing import Protocol
from uuid import UUID

from app.gateway.protocol import GatewayMessage


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

    async def register(self, player_id: UUID, connection_id: UUID, *, ttl_seconds: int) -> int:
        """Records an open connection. Returns the live count including it.

        A return of `1` means *this* connection is the player's first, and
        is the signal to mark them online. Idempotent on `connection_id`:
        registering the same one twice leaves one entry and a count that
        counts it once.
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

    async def refresh(self, player_id: UUID, connection_id: UUID, *, ttl_seconds: int) -> bool:
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


class ConnectionClosed(Exception):
    """The peer has gone.

    Raised by `send` and `receive`. Deliberately **not** an error: it is how
    a connection normally ends, and the lifecycle treats it as the exit
    condition of its read loop rather than as a failure to report.
    """


__all__ = [
    "ConnectionClosed",
    "ConnectionRegistry",
    "GatewaySocket",
    "RedeemedIdentity",
    "TicketRedeemer",
]
