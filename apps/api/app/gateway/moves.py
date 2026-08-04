"""`MoveSubmissionHandler` — one `game.move.submit`, end to end. A64-016.3.

The transport half of a move. Everything about the *rules* is
`SubmitMoveUseCase`'s; what is here is the order in which a frame is checked,
what a failure becomes on the wire, and who receives the result.

    rate limit  ->  decode  ->  room membership  ->  idempotency
                ->  game.public  ->  acknowledge  ->  fan out  ->  remember

## The order, and why each step is where it is

**Rate limit first** (§13 — "rate limiting occurs before expensive Game
Engine work"). A refused frame must cost a Redis increment, not a database
read and a move generation. Putting it after the decode would already be
wrong: decoding is cheap, but the check is what protects everything after it,
and every step moved above it is a step an attacker gets for free.

**Room membership before the game** (§4). The room check is one Redis read;
the game check is a database read plus a position load. Both refuse a
non-participant, so the cheap one goes first — and it also catches the client
that simply forgot to `room.join`, which is a different, actionable message.

**Idempotency before the game** (§7). A retry must not re-run the engine, and
must not re-run the *validation* either: a duplicate whose original was
rejected replays the rejection rather than re-deciding it against a position
that may have moved on.

**Remember after the fan-out**, not before. The stored answer is the frame
that was actually sent, so a retry replays what the client already saw. If
the connection dies between the two, the entry is simply never written and
the retry is processed fresh — which the ply compare-and-set makes safe.

## The submitter gets two frames, and that is deliberate

`game.move.accepted` correlated to their `request_id`, and
`game.move.applied` as one of the room's recipients. Merging them would mean
a client could not tell its own move from its opponent's without inspecting
the payload — and it would mean two code paths for advancing the board, one
of which is exercised far less often.

## Errors never carry an exception

§14 forbids leaking SQL errors, Redis errors, class names and stack traces.
The only way to guarantee that is for the wire message never to come from the
failure object: `_REJECTIONS` maps an exception *type* to a fixed code and a
fixed sentence, and anything unmapped is `internal_error` with the detail in
the log where the caller cannot read it.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from app.gateway.delivery import RoomBroadcaster
from app.gateway.event_buffer import RedisMatchEventBuffer
from app.gateway.metrics import (
    MOVE_SUBMISSIONS,
    MOVES_ACCEPTED,
    MOVES_REJECTED,
    MoveRejection,
)
from app.gateway.ports import MoveIdempotency, MoveRateLimiter
from app.gateway.protocol import (
    GatewayErrorCode,
    GatewayMessage,
    move_accepted,
    move_applied,
    move_rejected,
)
from app.gateway.room_service import GameRoomService
from app.gateway.spectators import SpectatorStore, SpectatorSubscription
from app.modules.game.public import (
    ClockExpired,
    IllegalMoveSubmitted,
    MatchNotActive,
    MatchNotFound,
    NotAMatchParticipant,
    NotYourTurn,
    StaleMatchState,
    SubmitMoveRequest,
    SubmitMoveResult,
    SubmitMoveUseCase,
)
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)

#: How a failure reaches the client — §14's error mapping.
#:
#: Keyed on the exception **type**, so a new failure in `game` that means
#: the same thing to a client is one line here rather than a new branch. The
#: sentence is fixed and safe: it never comes from the exception, which is
#: what makes "no SQL errors, no Redis errors, no class names, no stack
#: traces" a property of the table rather than a rule somebody remembers.
_REJECTIONS: Final[dict[type[Exception], tuple[GatewayErrorCode, MoveRejection, str]]] = {
    MatchNotFound: (
        GatewayErrorCode.NOT_A_PARTICIPANT,
        MoveRejection.NOT_A_PARTICIPANT,
        "That match is not yours.",
    ),
    NotAMatchParticipant: (
        GatewayErrorCode.NOT_A_PARTICIPANT,
        MoveRejection.NOT_A_PARTICIPANT,
        "That match is not yours.",
    ),
    MatchNotActive: (
        GatewayErrorCode.MATCH_NOT_ACTIVE,
        MoveRejection.MATCH_NOT_ACTIVE,
        "That match is not being played.",
    ),
    NotYourTurn: (
        GatewayErrorCode.NOT_YOUR_TURN,
        MoveRejection.NOT_YOUR_TURN,
        "It is not your turn.",
    ),
    IllegalMoveSubmitted: (
        GatewayErrorCode.ILLEGAL_MOVE,
        MoveRejection.ILLEGAL_MOVE,
        "That is not a legal move.",
    ),
    ClockExpired: (
        GatewayErrorCode.CLOCK_EXPIRED,
        MoveRejection.CLOCK_EXPIRED,
        "Your time ran out.",
    ),
    StaleMatchState: (
        GatewayErrorCode.STALE_STATE,
        MoveRejection.STALE_STATE,
        "The match moved on. Refresh and try again.",
    ),
}


class MoveSubmissionHandler:
    """Handles `game.move.submit` and nothing else.

    Holds five collaborators, which is the most of any handler on this tier
    and is why it is a class rather than a function: the dispatch table
    passes it a frame and an identity, and everything it needs to answer is
    already bound.
    """

    def __init__(
        self,
        *,
        moves: SubmitMoveUseCase,
        rooms: GameRoomService,
        broadcaster: RoomBroadcaster,
        buffer: RedisMatchEventBuffer,
        spectators: SpectatorStore,
        idempotency: MoveIdempotency,
        limiter: MoveRateLimiter,
        metrics: MetricsRecorder,
        idempotency_ttl_seconds: int,
    ) -> None:
        self._moves = moves
        self._rooms = rooms
        self._broadcaster = broadcaster
        self._buffer = buffer
        self._spectators = spectators
        self._idempotency = idempotency
        self._limiter = limiter
        self._metrics = metrics
        self._idempotency_ttl_seconds = idempotency_ttl_seconds

    async def handle(
        self,
        message: GatewayMessage,
        *,
        player_id: UUID,
        connection_id: UUID,
        received_at: datetime,
    ) -> GatewayMessage:
        """One submission. Returns the frame to send back to the submitter.

        Never raises. A handler that raised would reach the connection
        lifecycle's catch-all and close the socket, which for one bad move
        is exactly the overreaction §13 forbids for a rate-limit violation
        and is no more appropriate here.
        """
        self._metrics.increment(MOVE_SUBMISSIONS)

        if not await self._limiter.allow(connection_id):
            # Before the decode, before any read. A refused frame costs one
            # Redis increment — see this module's docstring on ordering.
            return self._refuse(
                GatewayErrorCode.RATE_LIMITED,
                MoveRejection.RATE_LIMITED,
                "Too many moves. Slow down.",
                request_id=message.request_id,
            )

        submission = _submission_of(message, player_id=player_id, received_at=received_at)
        if submission is None:
            return self._refuse(
                GatewayErrorCode.MALFORMED_MESSAGE,
                MoveRejection.MALFORMED,
                "That move could not be read.",
                request_id=message.request_id,
            )

        if not await self._rooms.is_attached(
            submission.match_id, player_id=player_id, connection_id=connection_id
        ):
            # Cheaper than the game check and a different message: this
            # client is a participant who forgot to `room.join`, which is
            # actionable, where "not your match" is not.
            return self._refuse(
                GatewayErrorCode.NOT_IN_ROOM,
                MoveRejection.NOT_IN_ROOM,
                "Join the match room before moving.",
                request_id=message.request_id,
            )

        replayed = await self._replayed(message, connection_id=connection_id)
        if replayed is not None:
            return replayed

        return await self._apply(submission, message, connection_id=connection_id)

    async def _apply(
        self, submission: SubmitMoveRequest, message: GatewayMessage, *, connection_id: UUID
    ) -> GatewayMessage:
        """Submits to `game`, acknowledges, and fans out."""
        try:
            result = await self._moves.submit(submission)
        except Exception as exc:  # noqa: BLE001 — every failure becomes a wire code
            answer = self._rejection_for(exc, request_id=message.request_id)
            await self._remember(message, connection_id=connection_id, frame=answer)
            return answer

        self._metrics.increment(MOVES_ACCEPTED)
        answer = _accepted_frame(result, request_id=message.request_id)

        # After the move is committed and before the acknowledgement is
        # returned — but the acknowledgement is the caller's to send, so a
        # fan-out failure cannot delay it past this function returning.
        await self._broadcast(result)
        await self._remember(message, connection_id=connection_id, frame=answer)
        return answer

    async def _broadcast(self, result: SubmitMoveResult) -> None:
        """Sends `game.move.applied` to both participants, and records the
        room's projection.

        **Never raises**, and never affects the answer: the move is already
        committed in `game` (§9, §10). A room whose projection could not be
        written or a socket that went away costs a client one
        resynchronisation, and the alternative — failing a move that was
        played — is strictly worse.
        """
        room = await self._rooms.room_of(result.match_id)
        if room is None:
            logger.warning("gateway_move_room_missing", extra={"match_id": str(result.match_id)})
            return

        await self._rooms.record_progress(
            result.match_id,
            ply=result.ply,
            side_to_move=result.side_to_move.value,
            fingerprint=result.fingerprint,
        )

        applied = move_applied(
            match_id=result.match_id,
            ply=result.ply,
            side_to_move=result.side_to_move.value,
            fingerprint=result.fingerprint,
            path=result.applied.path,
            captured=result.applied.captured,
            promoted_to=result.applied.promoted_to,
            result=_result_payload(result),
        )

        # **Buffered before it is delivered** — A64-016.6 §2, §3. A client
        # that reconnects a moment after a move must find it in the buffer,
        # and delivery is the slower of the two: it writes sockets and may
        # publish to another node. Buffering second would leave a window in
        # which the move is on somebody's screen and not in the replay.
        #
        # The frame is the *same string* that goes to a live socket, so a
        # resuming client applies bytes identical to the ones it would have
        # received — see `match_events` on why re-encoding would be a second
        # encoder able to disagree with the first.
        await self._buffer.append(result.match_id, sequence=result.ply, frame=applied.to_json())

        report = await self._broadcaster.deliver(
            applied,
            recipients=room.participants,
            spectators=await self._watching(result.match_id),
        )

        # One line per move rather than per recipient (§15), and no board,
        # no path and no fingerprint in it: the payload is the game.
        logger.info(
            "gateway_move_delivered",
            extra={
                "match_id": str(result.match_id),
                "ply": result.ply,
                "local": report.local,
                "remote_nodes": report.remote_nodes,
                "failures": report.failures,
                "spectator_failures": report.spectator_failures,
            },
        )

    async def _watching(self, match_id: UUID) -> Sequence[SpectatorSubscription]:
        """Who is spectating this match — A64-016.7 §5.

        A read on the move path, so it is bounded by the same posture the
        rest of this method has: a failure returns **no audience** rather
        than raising, because a spectator that missed a frame resynchronises
        by rejoining and a move that failed because a viewer list could not
        be read would be a game lost to somebody else's tab.
        """
        try:
            return await self._spectators.routes_for(match_id)
        except Exception as exc:  # noqa: BLE001 — an audience must not fail a move
            logger.warning(
                "gateway_spectator_routes_failed",
                extra={"match_id": str(match_id), "error": type(exc).__name__},
            )
            return ()

    async def _replayed(
        self, message: GatewayMessage, *, connection_id: UUID
    ) -> GatewayMessage | None:
        """The answer this `request_id` already produced, if any.

        `None` when the frame carries no `request_id` — there is nothing to
        key on, and inventing one would be the second correlation identifier
        §7 forbids.
        """
        if message.request_id is None:
            return None

        replayed = await self._idempotency.replay(connection_id, message.request_id)
        if replayed is not None:
            # Counted as neither accepted nor rejected: the outcome was
            # already counted the first time it happened, and counting it
            # again would make the accept rate depend on how often clients
            # retry. `submissions - accepted - rejected` is the duplicate
            # count, which is the number an operator actually wants.
            logger.debug("gateway_move_replayed")
        return replayed

    async def _remember(
        self, message: GatewayMessage, *, connection_id: UUID, frame: GatewayMessage
    ) -> None:
        if message.request_id is None:
            return

        await self._idempotency.remember(
            connection_id,
            message.request_id,
            frame=frame,
            ttl_seconds=self._idempotency_ttl_seconds,
        )

    def _rejection_for(self, error: Exception, *, request_id: str | None) -> GatewayMessage:
        """One failure as a wire message. See `_REJECTIONS`."""
        mapped = _REJECTIONS.get(type(error))
        if mapped is None:
            # Unmapped means unexpected. The exception goes to the log with
            # its type and traceback; the client is told nothing beyond
            # "internal", because anything more is a detail about the
            # server's internals (§14).
            logger.error(
                "gateway_move_failed",
                extra={"error": type(error).__name__},
                exc_info=error,
            )
            return self._refuse(
                GatewayErrorCode.INTERNAL_ERROR,
                MoveRejection.INTERNAL,
                "Something went wrong. Try again.",
                request_id=request_id,
            )

        code, category, reason = mapped
        return self._refuse(code, category, reason, request_id=request_id)

    def _refuse(
        self,
        code: GatewayErrorCode,
        category: MoveRejection,
        reason: str,
        *,
        request_id: str | None,
    ) -> GatewayMessage:
        self._metrics.increment(MOVES_REJECTED, labels={"category": category})
        return move_rejected(code, request_id=request_id, reason=reason)


def _result_payload(result: SubmitMoveResult) -> dict[str, str | None] | None:
    """The match result, when this move ended the game — A64-016.4 §7.

    `None` while the match continues, which is the overwhelmingly common
    answer. Rendered here rather than in `protocol` because it is a
    projection of `game`'s published result onto the wire, and the protocol
    module deliberately knows nothing about `game`.
    """
    if result.outcome is None or result.termination_reason is None:
        return None

    return {
        "outcome": result.outcome.value,
        "termination_reason": result.termination_reason.value,
        "winner": result.winner.value if result.winner is not None else None,
    }


def _accepted_frame(result: SubmitMoveResult, *, request_id: str | None) -> GatewayMessage:
    return move_accepted(
        match_id=result.match_id,
        ply=result.ply,
        side_to_move=result.side_to_move.value,
        fingerprint=result.fingerprint,
        path=result.applied.path,
        captured=result.applied.captured,
        promoted_to=result.applied.promoted_to,
        request_id=request_id,
        result=_result_payload(result),
    )


def _submission_of(
    message: GatewayMessage, *, player_id: UUID, received_at: datetime
) -> SubmitMoveRequest | None:
    """A decoded frame as a command, or `None` if the payload is not one.

    Validated here rather than in `protocol.decode`, because the envelope
    decoder knows nothing about what any particular payload should contain —
    and making it know would be the beginning of a schema registry inside
    the codec.

    **`player_id` and `received_at` come from the caller**, never the
    payload. The protocol has no field for either, so this is structural
    rather than remembered: there is no client-supplied identity or
    timestamp in scope to prefer by accident — and a client that could
    supply its own `received_at` could claim to have moved before its flag
    fell (A64-016.5 §2).
    """
    raw_match = message.payload.get("match_id")
    raw_path = message.payload.get("path")

    if not isinstance(raw_match, str) or not isinstance(raw_path, list):
        return None
    if len(raw_path) < 2 or not all(isinstance(square, str) for square in raw_path):
        return None

    try:
        match_id = UUID(raw_match)
    except ValueError:
        return None

    return SubmitMoveRequest(
        match_id=match_id,
        player_id=player_id,
        received_at=received_at,
        path=tuple(raw_path),
    )


__all__ = ["MoveSubmissionHandler"]
