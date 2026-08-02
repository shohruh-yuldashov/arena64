"""`GameRoomSession` — one match's routing scope. A64-016.2 §6.

A room is **not a game**. It holds no board, no clock, no move history and no
result; it answers one question — *which sockets are attached to this match* —
and A64-016.3 will use that answer to decide where a move confirmation goes.
R-7 is what makes that separation load-bearing: "the gateway contains no
domain logic … it never decides whether a move is legal", and a room that
knew anything about the contest would be the first place that stopped being
true.

## Ephemeral, and why that is not a shortcut

§6: "Keep it ephemeral. Do not persist rooms in PostgreSQL." AD-19 is the
rule behind it — nothing competitive lives only in Redis — and a room is the
clearest case of something that is *not* competitive: it is derived, at every
instant, from two facts that are each durable elsewhere. The participants
come from `game.match`, and the connections come from the gateway registry,
which is itself a claim about sockets that exist right now.

Losing every room costs a reconnect. Persisting them would mean a row per
match whose only truth is transient, and a reconciliation job to delete the
ones whose sockets closed while the process was down.

## Status is derived, not stored

`RoomStatus` is computed from the membership rather than written beside it,
because a stored status is a second copy of something the members already
say, and two copies of one fact is the drift `CLAUDE.md` §3.4 is about. The
interesting transition — "both players are here" — is exactly a predicate
over the member set, and computing it means it cannot be stale.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class RoomStatus(StrEnum):
    """How much of a match's routing scope is actually attached.

    Two members. A third — "closed" — was considered and rejected: a room
    with no members is indistinguishable from one that never existed, which
    is what an ephemeral store with a TTL gives for free, and a status
    nothing can observe is a state machine nobody maintains.
    """

    WAITING = "waiting"
    """At least one participant has no connection in the room.

    The state a room is in for the whole gap between the first player
    joining and the second — which on a real network is seconds, and which
    a client renders as "waiting for your opponent"."""

    READY = "ready"
    """Both participants have at least one connection attached.

    **Not "the game has started."** Nothing about this state activates a
    match or starts a clock (§8); it says the routing scope is complete, so
    a message sent to the room reaches both people."""


@dataclass(frozen=True, slots=True)
class RoomMember:
    """One connection attached to a room.

    The pair, not just the player, because a player may hold several — §8:
    "A player with multiple active connections may have more than one
    connection in the same room. Disconnecting one connection must not
    remove the player's other connections." A member keyed on the player
    alone could not express that, and the leave path would take every tab
    down with the first one closed.
    """

    player_id: UUID
    connection_id: UUID


@dataclass(frozen=True, slots=True)
class RoomProgress:
    """How far a match has got, as the room reports it — A64-016.3 §11.

    An **ephemeral read projection**, not a second source of truth. `game`
    owns the position (AD-18); this is the minimum a client needs to notice
    it has fallen behind and a router needs to order what it delivers.

    Three fields, and the restraint is the design. §11 says "do not
    duplicate the complete Game aggregate in `gwroom:v1:`", and a board here
    would be exactly that — a copy that can disagree with the authority,
    updated by a fan-out that is allowed to fail.
    """

    ply: int
    """The authoritative sequence, as last delivered. Monotonic: the store
    refuses a write that would move it backwards."""

    side_to_move: str
    """Whose turn it is, as a primitive. A string rather than `PlayerSide`
    because the gateway must not hold an engine enum — the same rule that
    keeps `SubmitMoveResult`'s applied move a tuple of strings."""

    fingerprint: str
    """A position fingerprint, for divergence detection. Opaque here: the
    gateway compares it and never parses it."""


@dataclass(frozen=True, slots=True)
class GameRoomSession:
    """One match's routing scope, as it stands right now.

    A **view**, built per read from the roster and the member set rather
    than a stored aggregate. That is what keeps `status` honest and what
    makes the room genuinely ephemeral — there is no object anywhere whose
    lifecycle has to be managed, only a set of members with a TTL.
    """

    match_id: UUID
    participants: tuple[UUID, ...]
    """The two players `game` says are in this match, in a stable order.

    From `game.public.MatchRoster` and never from the members: a room whose
    idea of "who is in this match" came from who happened to be connected
    would admit whoever joined first and call them a participant.
    """

    members: tuple[RoomMember, ...]
    observed_at: datetime
    """When this view was built.

    Kept because every field above is a claim about an instant — a member
    may have closed its socket since — and a caller acting on a stale view
    should be able to see how stale. The same reason
    `ReconciliationEntry.recorded_at` sits beside `occurred_at`.
    """

    @property
    def status(self) -> RoomStatus:
        """`READY` when every participant has at least one connection."""
        attached = {member.player_id for member in self.members}
        return (
            RoomStatus.READY
            if all(player_id in attached for player_id in self.participants)
            else RoomStatus.WAITING
        )

    @property
    def both_connected(self) -> bool:
        """§7's "check whether both players are connected", by its own name.

        An alias for `status is READY`, and it earns its place: the question
        is asked by callers that have no interest in the room's state
        machine, and `room.both_connected` reads as the predicate it is
        where a status comparison reads as bookkeeping.
        """
        return self.status is RoomStatus.READY

    def connections_of(self, player_id: UUID) -> tuple[UUID, ...]:
        """Every connection this player has in this room.

        Plural, always — see `RoomMember` on why the singular would be a
        bug rather than a simplification.
        """
        return tuple(
            member.connection_id for member in self.members if member.player_id == player_id
        )

    def includes(self, player_id: UUID, connection_id: UUID) -> bool:
        """Whether one specific connection is attached."""
        return RoomMember(player_id=player_id, connection_id=connection_id) in self.members

    @classmethod
    def of(
        cls,
        *,
        match_id: UUID,
        participants: Sequence[UUID],
        members: Sequence[RoomMember],
        observed_at: datetime,
    ) -> "GameRoomSession":
        """One view, from a roster and a member set."""
        return cls(
            match_id=match_id,
            participants=tuple(participants),
            members=tuple(members),
            observed_at=observed_at,
        )


__all__ = ["GameRoomSession", "RoomMember", "RoomProgress", "RoomStatus"]
