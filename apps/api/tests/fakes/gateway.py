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
from collections.abc import Sequence
from uuid import UUID

from app.gateway.ports import ConnectionClosed
from app.gateway.protocol import GatewayMessage, MessageType
from app.modules.users.public import DeviceType


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
        self.connections: dict[UUID, set[UUID]] = {}
        #: Makes every write raise, for the registration-failure path.
        self.fails = False
        self.unregister_calls: list[tuple[UUID, UUID]] = []

    async def register(self, player_id: UUID, connection_id: UUID, *, ttl_seconds: int) -> int:
        if self.fails:
            raise RuntimeError("the registry is unreachable")
        # A set, so registering the same connection twice leaves one entry —
        # which is what `ZADD` on an existing member does.
        self.connections.setdefault(player_id, set()).add(connection_id)
        return len(self.connections[player_id])

    async def unregister(self, player_id: UUID, connection_id: UUID) -> int:
        self.unregister_calls.append((player_id, connection_id))
        live = self.connections.setdefault(player_id, set())
        # `discard`, not `remove`: unregistering something already gone is a
        # no-op that still reports the true remaining count, which is what
        # makes the real adapter's cleanup idempotent.
        live.discard(connection_id)
        return len(live)

    async def refresh(self, player_id: UUID, connection_id: UUID, *, ttl_seconds: int) -> bool:
        return connection_id in self.connections.get(player_id, set())

    async def active_count(self, player_id: UUID) -> int:
        return len(self.connections.get(player_id, set()))


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
    "FakeConnectionRegistry",
    "FakeGatewaySocket",
    "FakeTicketRedeemer",
    "RecordingPresence",
]
