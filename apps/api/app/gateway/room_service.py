"""`GameRoomService` — who may attach a socket to a match. A64-016.2 §7, §8.

The one place in the gateway that says no. Everything else in this tier moves
frames; this decides whether a connection belongs in a match's routing scope,
and §7 gives the rule in three clauses:

    "Only actual Match participants may join the room."
    "Resolve participants through `game.public`."
    "Do not trust a client-supplied player ID. Use the authenticated socket
     identity."

The third is the one that would be easy to get wrong and impossible to
notice. `join` takes the player from its **caller**, which is the connection
lifecycle holding the identity a redeemed ticket proved — and `room.join`'s
payload carries a `match_id` and nothing else, so there is no client-supplied
player id anywhere in this path to accidentally prefer. That is structural
rather than remembered: the frame has no field for it.

## R-7 still holds

This decides membership, not gameplay. It reads `game.public.MatchRoster`
and compares two identifiers; it does not know what a move is, cannot make
one, and holds no capability that could change a match. `MatchRosterReader`
has exactly one method for that reason.

## Which match states have a room, and why that is decided here

Today: `active` alone. A match is `active` once both players accepted, which
is the first instant at which "route messages between these two" is a
sensible thing to want — before that the handshake is still an HTTP concern
and `PendingMatchView` serves it.

The rule lives here rather than in `GameMatchRoster` because it is the
*gateway's* policy over `game`'s published state. A later task that wants
both players attached during the handshake — to watch each other accept —
changes this predicate and nothing in `game`.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.gateway.metrics import (
    ROOM_JOIN_REJECTIONS,
    ROOM_JOINS,
    ROOM_LEAVES,
    ROOM_STATES,
    RoomLeaveReason,
    RoomRejectionReason,
)
from app.gateway.ports import RoomMemberStore
from app.gateway.rooms import GameRoomSession, RoomMember
from app.modules.game.public import MatchRecordStatus, MatchRosterReader
from app.modules.tournament.public import TournamentAttendance
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)

#: The match states that have a routing scope — see this module's docstring.
#:
#: A frozen set rather than an `is` comparison so that adding
#: `PENDING_ACCEPTANCE` later is one line here, at the place that already
#: explains why it is not there today.
ROOMABLE_STATES = frozenset({MatchRecordStatus.ACTIVE})


class RoomJoinRefused(Exception):
    """A socket may not attach to this room.

    Carries the *reason* as a member of `RoomRejectionReason`, which the
    caller maps to a wire code. An exception rather than a nullable return
    because the two outcomes are different kinds — "here is your room" and
    "you may not have one" — and a caller that forgot to check a `None`
    would proceed as though it had joined.
    """

    def __init__(self, reason: RoomRejectionReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class GameRoomService:
    """Join, leave, and what a room looks like."""

    def __init__(
        self,
        *,
        rosters: MatchRosterReader,
        members: RoomMemberStore,
        attendance: TournamentAttendance,
        metrics: MetricsRecorder,
        clock: Clock,
        room_ttl_seconds: int,
    ) -> None:
        self._rosters = rosters
        self._members = members
        self._attendance = attendance
        self._metrics = metrics
        self._clock = clock
        self._room_ttl_seconds = room_ttl_seconds

    async def join(
        self, match_id: UUID, *, player_id: UUID, connection_id: UUID
    ) -> GameRoomSession:
        """Attaches one authenticated connection to a match's room.

        `player_id` is the **socket's proven identity**, passed by the
        lifecycle — never read from the frame. See this module's docstring.

        Raises `RoomJoinRefused`:

            NOT_A_PARTICIPANT   the match does not exist, or this player is
                                not in it. **One reason for both**, because
                                distinguishing them makes live match
                                identifiers enumerable by response — the
                                same argument `MatchAcceptanceUseCase.accept`
                                makes for collapsing them into
                                `MatchNotFound`
            ROOM_UNAVAILABLE    a participant, but the match has no room in
                                its current state

        Idempotent: a client retrying after a dropped response is attached
        once and gets the same room back, because the store is idempotent on
        `(player_id, connection_id)`.
        """
        roster = await self._rosters.roster_of(match_id)
        if roster is None or not roster.includes(player_id):
            # No `match_id` in this log line. A refused join is the one
            # place an unauthenticated-shaped caller learns anything, and a
            # log full of the ids that were probed is a log that answers the
            # probe for anyone who reads it later.
            self._reject(RoomRejectionReason.NOT_A_PARTICIPANT, player_id=player_id)
            raise RoomJoinRefused(RoomRejectionReason.NOT_A_PARTICIPANT)

        if roster.status not in ROOMABLE_STATES:
            self._reject(RoomRejectionReason.ROOM_UNAVAILABLE, player_id=player_id)
            raise RoomJoinRefused(RoomRejectionReason.ROOM_UNAVAILABLE)

        attached = await self._members.join(
            match_id,
            RoomMember(player_id=player_id, connection_id=connection_id),
            ttl_seconds=self._room_ttl_seconds,
        )
        room = GameRoomSession.of(
            match_id=match_id,
            participants=(roster.light_player_id, roster.dark_player_id),
            members=attached,
            observed_at=self._clock.now(),
        )

        await self._record_attendance(match_id, player_id)

        self._metrics.increment(ROOM_JOINS)
        self._metrics.increment(ROOM_STATES, labels={"state": room.status})
        logger.info(
            "gateway_room_joined",
            extra={
                "user_id": str(player_id),
                "match_id": str(match_id),
                "room_status": room.status.value,
                "members": len(room.members),
            },
        )
        return room

    async def _record_attendance(self, match_id: UUID, player_id: UUID) -> None:
        """Tells a tournament that one of its players turned up — §6e.

        Called **after** the join has succeeded, so attendance records
        somebody who is actually in the room rather than somebody who tried.

        **Never fails a join.** A tournament that cannot be told is a
        no-show policy that may adjudicate somebody who was present — bad,
        and strictly better than refusing to let two people play because a
        write to another module's table failed. The `ERROR` is what makes it
        visible, and the sweep's own re-read of `game` is what keeps a
        started match safe even then.

        A match no tournament owns matches no row and this changes nothing;
        see `tournament.public.attendance` on why that is one guarded
        statement rather than a lookup on every join.
        """
        try:
            await self._attendance.mark_present(match_id, player_id, at=self._clock.now())
        except Exception as exc:  # noqa: BLE001 — a join must not fail on this
            logger.error(
                "gateway_attendance_write_failed",
                extra={
                    "match_id": str(match_id),
                    "user_id": str(player_id),
                    "error": type(exc).__name__,
                },
                exc_info=exc,
            )

    async def leave(
        self, match_id: UUID, *, player_id: UUID, connection_id: UUID
    ) -> GameRoomSession | None:
        """Detaches one connection. `None` if the match no longer resolves.

        **Idempotent** (§8): leaving a room this connection is not in is not
        an error and produces the same observable outcome — the connection
        is not in the room. A client that retries, or one whose disconnect
        cleanup races its own `room.leave`, gets the answer it asked for.

        Detaches **one connection**, never a player: §8 requires that
        disconnecting one tab leaves the player's others in the room, and
        that is held by the member being the pair rather than the player.
        """
        remaining = await self._members.leave(
            match_id, RoomMember(player_id=player_id, connection_id=connection_id)
        )
        self._metrics.increment(ROOM_LEAVES, labels={"reason": RoomLeaveReason.CLIENT})
        logger.info(
            "gateway_room_left",
            extra={
                "user_id": str(player_id),
                "match_id": str(match_id),
                "members": len(remaining),
            },
        )

        roster = await self._rosters.roster_of(match_id)
        if roster is None:
            # The match was pruned while a socket was still attached to its
            # room. The detach above already happened, which is the part
            # that matters; there is simply no roster left to describe.
            return None

        return GameRoomSession.of(
            match_id=match_id,
            participants=(roster.light_player_id, roster.dark_player_id),
            members=remaining,
            observed_at=self._clock.now(),
        )

    async def detach(self, *, player_id: UUID, connection_id: UUID) -> int:
        """Removes one connection from every room it is in. Returns how many.

        The **disconnect** path, and the reason `RoomMemberStore` carries a
        reverse index. A socket that dropped never sends `room.leave`, and a
        member left behind makes `both_connected` report a player who is not
        there — which is the single question the room exists to answer.

        Never raises: it runs from the connection lifecycle's cleanup, on
        every path including one already handling a failure, and an
        exception there would skip the unregister that has to happen
        regardless. A room member left behind lapses on its own TTL, which
        is the backstop this design already has for a node that died.
        """
        member = RoomMember(player_id=player_id, connection_id=connection_id)
        try:
            left = await self._members.leave_all(member)
        except Exception as exc:  # noqa: BLE001 — cleanup must not escalate
            logger.error(
                "gateway_room_detach_failed",
                extra={"user_id": str(player_id), "error": type(exc).__name__},
                exc_info=exc,
            )
            return 0

        if left:
            self._metrics.increment(
                ROOM_LEAVES, labels={"reason": RoomLeaveReason.DISCONNECT}, by=len(left)
            )
            logger.info(
                "gateway_rooms_detached",
                extra={"user_id": str(player_id), "rooms": len(left)},
            )
        return len(left)

    async def is_attached(self, match_id: UUID, *, player_id: UUID, connection_id: UUID) -> bool:
        """Whether **this connection** is in this match's room — §4.

        The connection, not the player. §4 requires the move path to
        "verify the connection belongs to the Game Room", and the
        distinction is real: a player with two tabs may have joined on one
        and not the other, and accepting a move from the tab that never
        joined would mean the fan-out has no route back to it.

        One Redis read, which is why the move handler asks this **before**
        it asks `game` anything — see `MoveSubmissionHandler` on the
        ordering.
        """
        members = await self._members.members_of(match_id)
        return RoomMember(player_id=player_id, connection_id=connection_id) in members

    async def record_progress(
        self, match_id: UUID, *, ply: int, side_to_move: str, fingerprint: str
    ) -> bool:
        """Records the room's live projection — §11.

        Forwards to the store's monotonic write and **never raises**: it
        runs after a move is already committed in `game`, so a projection
        that could not be written costs a client one resynchronisation, and
        failing a move that was played would be strictly worse.

        `False` means a newer ply is already stored, which under
        at-least-once fan-out is an ordinary race rather than a problem.
        """
        try:
            return await self._members.record_progress(
                match_id, ply=ply, side_to_move=side_to_move, fingerprint=fingerprint
            )
        except Exception as exc:  # noqa: BLE001 — a projection must not fail a move
            logger.warning(
                "gateway_room_progress_write_failed",
                extra={"match_id": str(match_id), "error": type(exc).__name__},
            )
            return False

    async def room_of(self, match_id: UUID) -> GameRoomSession | None:
        """The room as it stands, or `None` for a match that does not exist.

        The read behind §7's "list connected participants" and "check
        whether both players are connected" — both of which are properties
        of `GameRoomSession` rather than methods here, because they are
        questions about a view and not operations on a store.
        """
        roster = await self._rosters.roster_of(match_id)
        if roster is None:
            return None

        return GameRoomSession.of(
            match_id=match_id,
            participants=(roster.light_player_id, roster.dark_player_id),
            members=await self._members.members_of(match_id),
            observed_at=self._clock.now(),
        )

    def _reject(self, reason: RoomRejectionReason, *, player_id: UUID) -> None:
        self._metrics.increment(ROOM_JOIN_REJECTIONS, labels={"reason": reason})
        logger.info(
            "gateway_room_join_refused",
            extra={"user_id": str(player_id), "reason": reason.value},
        )


__all__ = ["ROOMABLE_STATES", "GameRoomService", "RoomJoinRefused"]
