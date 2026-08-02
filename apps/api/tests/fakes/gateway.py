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

from app.gateway.ports import ConnectionClosed, ConnectionRoute
from app.gateway.protocol import GatewayMessage, MessageType
from app.gateway.rooms import RoomMember
from app.modules.game.public import MatchRecordStatus, MatchRoster
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
