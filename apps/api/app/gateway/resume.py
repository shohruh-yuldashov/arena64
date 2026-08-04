"""`ResumeHandler` — putting a reconnecting client back where it was.
A64-016.6 §4, §5, §6.

    authenticate  ->  verify participation  ->  events or snapshot
                  ->  rejoin the room  ->  resumed

## Events or snapshot, and why the buffer decides rather than the client

§6 asks for clear rules and forbids "silent partial recovery". There is
exactly one question — *can the buffer prove it holds every event the client
missed* — and `BufferedEvents.is_contiguous` is that answer:

    contiguous          send the frames, in order
    not contiguous      send a snapshot, whose sequence becomes the new
                        baseline
    no sequence given   send a snapshot; the client is asking to start over

A client that reports a sequence *ahead* of the server's is treated as
needing a snapshot too. It cannot happen against a correct server, and the
only way it could is a client that saw a frame from a match that was rolled
back — for which "here is the truth" is the only safe answer.

The buffer is checked before the snapshot is built, because a snapshot is a
replay of the whole log and the common reconnect misses one or two plies.

## Cross-node resume needs nothing special

§5 asks for it and the honest answer is that it already works: the connection
registry is fleet-wide (`gwconn:v2:`), room membership is fleet-wide
(`gwroom:v1:`), the event buffer is in Redis, and the snapshot is built from
PostgreSQL. Nothing a resume touches is process-local, so a client that
reconnects to a different node reads exactly the same state.

What the new node does is register its own connection and join the room —
which is the ordinary join path, unchanged. The old connection is left to its
own cleanup or its TTL, which A64-016.1 §7 and A64-016.2 §8 already
guarantee: **no socket moves between nodes**, and none needs to.

## Idempotency

§8. Two resume frames with the same `request_id` are safe because every step
is: the room join is idempotent on `(player, connection)`, the buffer read
has no side effect, and the snapshot is a read. There is deliberately **no**
`request_id` cache here, unlike the move path — a move applies something and
a resume does not, so replaying an answer would be caching a read.

The one thing that must not double is room membership, and that is the
store's guarantee rather than this handler's.
"""

import logging
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.gateway.event_buffer import RedisMatchEventBuffer
from app.gateway.metrics import (
    RESUMES,
    ResumeOutcome,
)
from app.gateway.projections import snapshot_payload
from app.gateway.protocol import (
    GatewayErrorCode,
    GatewayMessage,
    error,
    match_events,
    match_snapshot,
    resumed,
    resync_required,
)
from app.gateway.room_service import GameRoomService, RoomJoinRefused
from app.modules.game.public import MatchSnapshot, MatchSnapshotReader
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)

#: The sequence a client reports when it has nothing — "start me over".
#:
#: Zero rather than absent, because zero is also the sequence of a match
#: nobody has moved in: a client resuming a brand-new game and one resuming
#: from nothing want the same answer, and giving them one code path is
#: cheaper than distinguishing two identical cases.
NO_SEQUENCE: Final = 0


@dataclass(frozen=True, slots=True)
class _Resumption:
    """A decoded `game.resume`. Never carries a player — see `_request_of`."""

    match_id: UUID
    last_known_sequence: int


class ResumeHandler:
    """Handles `game.resume` and nothing else."""

    def __init__(
        self,
        *,
        snapshots: MatchSnapshotReader,
        events: RedisMatchEventBuffer,
        rooms: GameRoomService,
        metrics: MetricsRecorder,
    ) -> None:
        self._snapshots = snapshots
        self._events = events
        self._rooms = rooms
        self._metrics = metrics

    async def handle(
        self, message: GatewayMessage, *, player_id: UUID, connection_id: UUID
    ) -> GatewayMessage:
        """One resume. Returns the frame to send back.

        Never raises. A handler that raised would reach the lifecycle's
        catch-all and close the socket — which for a client that is already
        recovering from a disconnect is the worst possible response.
        """
        request = _request_of(message)
        if request is None:
            return error(
                GatewayErrorCode.MALFORMED_MESSAGE,
                request_id=message.request_id,
                channel=message.channel,
            )

        snapshot = await self._snapshots.snapshot_of(request.match_id)
        if snapshot is None or not snapshot.includes(player_id):
            # One answer for both, so live match identifiers stay
            # unenumerable — the same rule the room join and the move path
            # already keep.
            self._reject(ResumeOutcome.NOT_A_PARTICIPANT)
            return error(
                GatewayErrorCode.NOT_A_PARTICIPANT,
                request_id=message.request_id,
                channel=message.channel,
            )

        rejoined = await self._rejoin(
            request.match_id, player_id=player_id, connection_id=connection_id
        )

        recovery = await self._recover(request, snapshot, request_id=message.request_id)
        if recovery is not None:
            return recovery

        return resumed(
            match_id=request.match_id,
            sequence=snapshot.sequence,
            both_connected=rejoined,
            request_id=message.request_id,
        )

    async def _recover(
        self, request: _Resumption, snapshot: MatchSnapshot, *, request_id: str | None
    ) -> GatewayMessage | None:
        """The frames or the snapshot, or `None` if the client is current.

        `None` is the fast path and the common one: a client that dropped
        and returned within a second has missed nothing, and sending it a
        snapshot it already has would make every flaky network a full
        replay.
        """
        if request.last_known_sequence >= snapshot.sequence:
            self._record(ResumeOutcome.CURRENT)
            return None

        if request.last_known_sequence <= NO_SEQUENCE:
            self._record(ResumeOutcome.SNAPSHOT)
            return match_snapshot(snapshot_payload(snapshot), request_id=request_id)

        buffered = await self._events.since(request.match_id, sequence=request.last_known_sequence)
        if not buffered.is_contiguous:
            # §6: no silent partial recovery. The client is told to ask
            # again from nothing rather than being handed a snapshot it did
            # not request, so it can count how often it falls behind.
            self._record(ResumeOutcome.RESYNC_REQUIRED)
            return resync_required(match_id=request.match_id, request_id=request_id)

        self._record(ResumeOutcome.INCREMENTAL)
        return match_events(
            match_id=request.match_id, frames=buffered.frames, request_id=request_id
        )

    async def _rejoin(self, match_id: UUID, *, player_id: UUID, connection_id: UUID) -> bool:
        """Puts this connection back in the room. Idempotent — §8.

        A refusal is **not** fatal here, unlike on the join path: the client
        has been proven a participant by the snapshot, so the only reason
        the room can refuse is that the match is no longer in a state that
        has one — a game that finished while they were away. They still get
        their snapshot, which is how they learn it is over.
        """
        try:
            room = await self._rooms.join(
                match_id, player_id=player_id, connection_id=connection_id
            )
        except RoomJoinRefused:
            logger.info("gateway_resume_room_unavailable", extra={"match_id": str(match_id)})
            return False

        return room.both_connected

    def _record(self, outcome: ResumeOutcome) -> None:
        self._metrics.increment(RESUMES, labels={"outcome": outcome})

    def _reject(self, outcome: ResumeOutcome) -> None:
        self._metrics.increment(RESUMES, labels={"outcome": outcome})
        logger.info("gateway_resume_refused", extra={"outcome": outcome.value})


def _request_of(message: GatewayMessage) -> _Resumption | None:
    """A decoded frame as a resumption, or `None` if it is not one.

    **No player id**, and the frame has no field for one (§4 — "do not trust
    player identity from payload"). The identity is the socket's redeemed
    ticket, which is structural rather than remembered: there is nothing
    client-supplied in scope to prefer by accident.

    A negative sequence is treated as absent rather than refused: it is a
    client bug, and the safe reading of "I am at ply minus one" is "I have
    nothing".
    """
    raw_match = message.payload.get("match_id")
    if not isinstance(raw_match, str):
        return None

    raw_sequence = message.payload.get("last_known_sequence", NO_SEQUENCE)
    if not isinstance(raw_sequence, int) or isinstance(raw_sequence, bool):
        return None

    try:
        match_id = UUID(raw_match)
    except ValueError:
        return None

    return _Resumption(match_id=match_id, last_known_sequence=max(NO_SEQUENCE, raw_sequence))


__all__ = ["NO_SEQUENCE", "ResumeHandler"]
