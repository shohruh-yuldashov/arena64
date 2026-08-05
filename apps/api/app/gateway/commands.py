"""`GameCommandHandler` — resign and the three draw frames, end to end.
A64-020.5C-pre §8.

The transport half of a participant command. Everything about *what a
command means* is `GameCommandUseCase`'s; what is here is the order in which
a frame is checked, what a failure becomes on the wire, and who receives the
result.

    rate limit  ->  decode  ->  room membership  ->  idempotency
                ->  game.public  ->  acknowledge  ->  fan out  ->  remember

Deliberately the identical order to `MoveSubmissionHandler`, and the reasons
transfer unchanged: the rate limit protects everything after it, the room
check is one Redis read against the game check's database read, and the
idempotency replay must precede the use case so a retry cannot re-decide
against state that moved on.

**One handler for four frames.** They differ by a single enum value and
share every step, so four handlers would be four copies of this pipeline —
and the copy that forgot to check room membership would be a stranger
resigning somebody's game. The `MessageType` is mapped to a `GameCommand`
by one table, which is small enough to read and closed enough to check.

## The fan-out is not the same set for every command

    game.completed      both participants **and** the audience. A finished
                        game is public
    game.draw.offered   participants only
    game.draw.declined  participants only

The split is enforced by `SPECTATOR_SAFE_EVENTS` inside `RoomBroadcaster`
rather than by this module choosing a recipient list, which is §8's "do not
accidentally deliver offer controls to spectators through the unified
recipient set": the spectator argument is passed the same way for every
frame, and the allowlist is what actually withholds the private ones. A call
site cannot leak by forgetting, because a call site is not what decides.

## Errors never carry an exception

§13 asks for bounded stable codes and forbids leaking constraint names,
Python exception names and SQL. The only way to guarantee that is for the
wire message never to come from the failure object: `_REJECTIONS` maps an
exception *type* to a fixed code and a fixed sentence, and anything unmapped
is `internal_error` with the detail in the log where the caller cannot read
it. The same mechanism `moves.py` uses, for the same reason.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from app.gateway.delivery import RoomBroadcaster
from app.gateway.metrics import (
    GAME_COMMANDS,
    GAME_COMMANDS_REJECTED,
    GameCommandRejection,
)
from app.gateway.ports import MoveIdempotency, MoveRateLimiter
from app.gateway.protocol import (
    GatewayErrorCode,
    GatewayMessage,
    MessageType,
    command_rejected,
    draw_declined,
    draw_offered,
    game_completed,
)
from app.gateway.room_service import GameRoomService
from app.gateway.spectators import SpectatorStore, SpectatorSubscription
from app.modules.game.public import (
    DrawOfferAlreadyPending,
    DrawOfferNotAllowedYet,
    DrawOfferNotPending,
    DrawOfferNotRecipient,
    GameCommand,
    GameCommandRequest,
    GameCommandResult,
    GameCommandUseCase,
    MatchNotActive,
    MatchNotFound,
    NotAMatchParticipant,
    StaleMatchState,
)
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)

#: Which command each frame asks for — §8.
#:
#: The whole of the routing decision, and a mapping rather than a chain of
#: branches so that "which frames does this handler serve" is one readable
#: literal. A frame that reaches `handle` without an entry here is a
#: dispatch-table mistake, and it is answered as malformed rather than
#: raising — see `handle`.
_COMMANDS: Final[Mapping[MessageType, GameCommand]] = {
    MessageType.RESIGN: GameCommand.RESIGN,
    MessageType.DRAW_OFFER: GameCommand.OFFER_DRAW,
    MessageType.DRAW_ACCEPT: GameCommand.ACCEPT_DRAW,
    MessageType.DRAW_DECLINE: GameCommand.DECLINE_DRAW,
}

#: One failure as a wire code, a metric category and a fixed sentence.
#:
#: `MatchNotFound` and `NotAMatchParticipant` share a code deliberately: a
#: client must not be able to tell "no such match" from "not yours", or live
#: match identifiers become enumerable by sending resignations at them —
#: the same rule the room join, the move path and the resume path all keep.
_REJECTIONS: Final[Mapping[type[Exception], tuple[GatewayErrorCode, GameCommandRejection, str]]] = {
    MatchNotFound: (
        GatewayErrorCode.NOT_A_PARTICIPANT,
        GameCommandRejection.NOT_A_PARTICIPANT,
        "That match is not yours.",
    ),
    NotAMatchParticipant: (
        GatewayErrorCode.NOT_A_PARTICIPANT,
        GameCommandRejection.NOT_A_PARTICIPANT,
        "That match is not yours.",
    ),
    MatchNotActive: (
        GatewayErrorCode.MATCH_NOT_ACTIVE,
        GameCommandRejection.MATCH_NOT_ACTIVE,
        "That match is not being played.",
    ),
    DrawOfferAlreadyPending: (
        GatewayErrorCode.DRAW_OFFER_ALREADY_PENDING,
        GameCommandRejection.DRAW_OFFER_ALREADY_PENDING,
        "A draw offer already stands.",
    ),
    DrawOfferNotPending: (
        GatewayErrorCode.DRAW_OFFER_NOT_PENDING,
        GameCommandRejection.DRAW_OFFER_NOT_PENDING,
        "There is no draw offer to answer.",
    ),
    DrawOfferNotRecipient: (
        GatewayErrorCode.DRAW_OFFER_NOT_RECIPIENT,
        GameCommandRejection.DRAW_OFFER_NOT_RECIPIENT,
        "You cannot answer your own draw offer.",
    ),
    DrawOfferNotAllowedYet: (
        GatewayErrorCode.DRAW_OFFER_NOT_ALLOWED_YET,
        GameCommandRejection.DRAW_OFFER_NOT_ALLOWED_YET,
        "Wait for your opponent to move before offering again.",
    ),
    StaleMatchState: (
        GatewayErrorCode.STALE_STATE,
        GameCommandRejection.STALE_STATE,
        "The match moved on. Re-read and try again.",
    ),
}


class GameCommandHandler:
    """Handles `game.resign` and the three `game.draw.*` frames."""

    def __init__(
        self,
        *,
        commands: GameCommandUseCase,
        rooms: GameRoomService,
        broadcaster: RoomBroadcaster,
        spectators: SpectatorStore,
        idempotency: MoveIdempotency,
        limiter: MoveRateLimiter,
        metrics: MetricsRecorder,
        idempotency_ttl_seconds: int,
    ) -> None:
        self._commands = commands
        self._rooms = rooms
        self._broadcaster = broadcaster
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
        """One command. Returns the frame to send back to the actor.

        Never raises. A handler that raised would reach the connection
        lifecycle's catch-all and close the socket — which for a player who
        mistyped a draw offer is an overreaction, and for one who just
        resigned is a socket closed on the frame that told them so.
        """
        command = _COMMANDS.get(message.type)
        if command is None:  # pragma: no cover — a dispatch-table mistake
            return self._refuse(
                GatewayErrorCode.MALFORMED_MESSAGE,
                GameCommandRejection.MALFORMED,
                "That command could not be read.",
                request_id=message.request_id,
            )

        self._metrics.increment(GAME_COMMANDS, labels={"command": command.value})

        # The move limiter, shared rather than duplicated. A command is
        # cheaper than a move and rarer, so a separate budget would be a
        # second thing to tune for a path that cannot outpace the one it
        # sits beside — and sharing means a client cannot dodge the move
        # limit by alternating moves with draw offers.
        if not await self._limiter.allow(connection_id):
            return self._refuse(
                GatewayErrorCode.RATE_LIMITED,
                GameCommandRejection.RATE_LIMITED,
                "Too many requests. Slow down.",
                request_id=message.request_id,
            )

        match_id = _match_id_of(message)
        if match_id is None:
            return self._refuse(
                GatewayErrorCode.MALFORMED_MESSAGE,
                GameCommandRejection.MALFORMED,
                "That command could not be read.",
                request_id=message.request_id,
            )

        if not await self._rooms.is_attached(
            match_id, player_id=player_id, connection_id=connection_id
        ):
            # Cheaper than the game check and a different message: this
            # client is a participant who forgot to `room.join`, which is
            # actionable, where "not your match" is not. It is also what
            # keeps a **spectator** out — a viewer is in the audience, not
            # in the room, so this refuses them before `game` is asked.
            return self._refuse(
                GatewayErrorCode.NOT_IN_ROOM,
                GameCommandRejection.NOT_IN_ROOM,
                "Join the match room first.",
                request_id=message.request_id,
            )

        replayed = await self._replayed(message, connection_id=connection_id)
        if replayed is not None:
            return replayed

        return await self._apply(
            GameCommandRequest(
                match_id=match_id,
                player_id=player_id,
                command=command,
                received_at=received_at,
            ),
            message,
            connection_id=connection_id,
        )

    async def _apply(
        self, request: GameCommandRequest, message: GatewayMessage, *, connection_id: UUID
    ) -> GatewayMessage:
        """Runs the command, acknowledges, and fans out."""
        try:
            result = await self._commands.execute(request)
        except Exception as exc:  # noqa: BLE001 — every failure becomes a wire code
            answer = self._rejection_for(exc, request_id=message.request_id)
            await self._remember(message, connection_id=connection_id, frame=answer)
            return answer

        answer = _authoritative_frame(result, request_id=message.request_id)
        await self._broadcast(result)
        await self._remember(message, connection_id=connection_id, frame=answer)
        return answer

    async def _broadcast(self, result: GameCommandResult) -> None:
        """Sends the authoritative event to the room.

        **Never raises**, and never affects the answer: the command is
        already committed in `game`. A room whose projection went missing
        or a socket that went away costs a client one resynchronisation,
        and the alternative — failing a resignation that already happened —
        is strictly worse.

        Not buffered into `RedisMatchEventBuffer`, unlike a move, and the
        reason is the buffer's own contract: it is keyed by the match
        sequence, and none of these commands advances the ply. Two entries
        at one sequence would break the contiguity check a resume depends
        on. A client that misses one recovers by the path §9 provides — the
        snapshot, which carries the offer and the result.
        """
        room = await self._rooms.room_of(result.match_id)
        if room is None:
            logger.warning("gateway_command_room_missing", extra={"match_id": str(result.match_id)})
            return

        report = await self._broadcaster.deliver(
            _authoritative_frame(result, request_id=None),
            recipients=room.participants,
            # Passed for every frame. Which of them an audience actually
            # receives is `SPECTATOR_SAFE_EVENTS`' decision, not this call
            # site's — see this module's docstring.
            spectators=await self._watching(result.match_id),
        )

        logger.info(
            "gateway_command_delivered",
            extra={
                "match_id": str(result.match_id),
                "command": result.command.value,
                "local": report.local,
                "remote_nodes": report.remote_nodes,
                "failures": report.failures,
                "spectator_failures": report.spectator_failures,
            },
        )

    async def _watching(self, match_id: UUID) -> Sequence[SpectatorSubscription]:
        """Who is spectating this match.

        A failure returns **no audience** rather than raising, the same
        posture the move path takes: a spectator that missed a frame
        resynchronises by rejoining, and a resignation that failed because
        a viewer list could not be read would be a game lost to somebody
        else's tab.
        """
        try:
            return await self._spectators.routes_for(match_id)
        except Exception as exc:  # noqa: BLE001 — an audience must not fail a command
            logger.warning(
                "gateway_spectator_routes_failed",
                extra={"match_id": str(match_id), "error": type(exc).__name__},
            )
            return ()

    async def _replayed(
        self, message: GatewayMessage, *, connection_id: UUID
    ) -> GatewayMessage | None:
        """The answer this `request_id` already produced, if any.

        Matters more here than on the move path. A move is naturally
        idempotent — the second one is refused by `uq_move__ply` — but a
        resignation resent after a dropped acknowledgement would otherwise
        reach a completed match and come back as `match_not_active`, which
        is a confusing answer to a command that succeeded.
        """
        if message.request_id is None:
            return None
        return await self._idempotency.replay(connection_id, message.request_id)

    async def _remember(
        self, message: GatewayMessage, *, connection_id: UUID, frame: GatewayMessage
    ) -> None:
        """Stores the answer this `request_id` produced.

        After the fan-out, so what is replayed is the frame the client
        actually saw. A failure here costs a retry being processed fresh,
        which the row lock and the status check make safe.
        """
        if message.request_id is None:
            return
        try:
            await self._idempotency.remember(
                connection_id,
                message.request_id,
                frame=frame,
                ttl_seconds=self._idempotency_ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — a cache must not fail a command
            logger.warning(
                "gateway_command_idempotency_write_failed",
                extra={"error": type(exc).__name__},
            )

    def _rejection_for(self, error_: Exception, *, request_id: str | None) -> GatewayMessage:
        """One failure as a wire message. See `_REJECTIONS`."""
        mapped = _REJECTIONS.get(type(error_))
        if mapped is None:
            # Unmapped means unexpected. The exception goes to the log with
            # its type and traceback; the client is told nothing beyond
            # "internal", because anything more is a detail about the
            # server's internals (§13).
            logger.error(
                "gateway_command_failed",
                extra={"error": type(error_).__name__},
                exc_info=error_,
            )
            return self._refuse(
                GatewayErrorCode.INTERNAL_ERROR,
                GameCommandRejection.INTERNAL,
                "Something went wrong. Try again.",
                request_id=request_id,
            )

        code, category, reason = mapped
        return self._refuse(code, category, reason, request_id=request_id)

    def _refuse(
        self,
        code: GatewayErrorCode,
        category: GameCommandRejection,
        reason: str,
        *,
        request_id: str | None,
    ) -> GatewayMessage:
        self._metrics.increment(GAME_COMMANDS_REJECTED, labels={"category": category})
        return command_rejected(code, request_id=request_id, reason=reason)


def _authoritative_frame(result: GameCommandResult, *, request_id: str | None) -> GatewayMessage:
    """The event this command produced.

    One function for the acknowledgement and the fan-out, so the two cannot
    describe the same command differently — the same reason
    `_applied_payload` is shared on the move path. The only difference is
    the correlation: the actor's copy carries their `request_id` and the
    broadcast carries none.
    """
    if result.is_terminal:
        return game_completed(
            match_id=result.match_id,
            ply=result.ply,
            result=_result_payload(result),
            request_id=request_id,
        )

    if result.offer is not None:
        return draw_offered(
            match_id=result.match_id,
            offered_by=result.offer.offered_by.value,
            offered_at_ply=result.offer.offered_at_ply,
            offered_at=result.offer.offered_at.isoformat(),
            request_id=request_id,
        )

    return draw_declined(
        match_id=result.match_id,
        declined_by=result.acting_side.value,
        ply=result.ply,
        request_id=request_id,
    )


def _result_payload(result: GameCommandResult) -> dict[str, Any]:
    """The terminal result, in the shape every other frame uses.

    Identical keys to `game.move.applied`'s `result` and `game.snapshot`'s,
    so a client parses one result payload everywhere.
    """
    return {
        "outcome": result.outcome.value if result.outcome else None,
        "termination_reason": (
            result.termination_reason.value if result.termination_reason else None
        ),
        "winner": result.winner.value if result.winner else None,
    }


def _match_id_of(message: GatewayMessage) -> UUID | None:
    """The frame's match, or `None` if it does not name one readably.

    **No player id and no side**, and the frames have no field for either
    (§7). The identity is the socket's redeemed ticket, which is structural
    rather than remembered: there is nothing client-supplied in scope to
    prefer by accident.
    """
    raw = message.payload.get("match_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


__all__ = ["GameCommandHandler"]
