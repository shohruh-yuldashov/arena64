"""`QuickMessageHandler` — `game.quick_message.send`, end to end. A64-023.1 §3, §4.

    rate limit  ->  decode + catalogue  ->  room membership
                ->  roster: participation and liveness  ->  fan out

The same order `MoveSubmissionHandler` and `GameCommandHandler` use, with
the reasons transferring unchanged: the rate limit protects everything after
it, and the checks run cheapest first — an in-memory enum lookup, then one
Redis read, then one database read. A frame naming something that is not in
the catalogue never reaches storage at all.

## Why the roster read is not optional

Room membership is *not* sufficient authorization, and the gap is easy to
miss. `ROOMABLE_STATES` is checked when a connection **joins**; a room then
outlives the match that made it, until its members leave or its TTL lapses.
So a connection attached to a match that has since finished is still
`is_attached`, and a handler that stopped there would carry conversation
into a completed game — the one thing §1(G) forbids.

`MatchRoster` answers both remaining questions in one read: `includes` is
the participation check, and `status` is the liveness check. It is also the
*only* `game` capability this handler holds — one read, no ability to
change anything, which is what keeps R-7 true of a transport tier that has
just gained a new inbound frame.

## Why there is no acknowledgement

The handler returns `None` on success, which `MessageHandler` documents as
"nothing to send back" and names as the seam a fire-and-forget message would
use. This is that message.

The sender learns their message went out by **receiving it**: the fan-out
goes to the room's participants, which includes them, so one client code
path renders a bubble whoever sent it. That is `move_applied`'s argument for
not merging a broadcast into an acknowledgement, and it is stronger here —
a correlated acknowledgement *plus* a broadcast would deliver the sender two
frames for one message, and the second is a duplicate bubble.

A **refusal** is still correlated, as `game.command.rejected` carrying the
frame's `request_id`, because a client that cannot tell which of two
in-flight sends was refused would have to guess.

## Why there is no idempotency entry

`MoveSubmissionHandler` and `GameCommandHandler` both remember one answer
per `(connection, request_id)`; this does not, and the difference is what a
replay would protect. A resignation resent after a dropped acknowledgement
must not reach a completed match and come back as `match_not_active` — the
command *succeeded*, and the confusing answer is the problem. A quick
message has no answer to replay and no state to protect: the worst case of
a retry is that the opponent sees "nice move" twice, which the rate limiter
already bounds and which no player can distinguish from somebody pressing
the button twice.

Not writing it is also what keeps §9's last property true — no storage grows
in proportion to quick-message traffic. An idempotency entry per message
would have been exactly that.

## Nothing here can fail a move

Every failure below becomes a wire code and returns; nothing raises past
this class, and nothing it touches is on the move path. The rate limiter is
a separate budget (`quick_message_limits`), the fan-out never raises, and
the handler writes to no store the game reads. A quick message that fails
costs a bubble.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from app.gateway.delivery import RoomBroadcaster
from app.gateway.metrics import QUICK_MESSAGES, QuickMessageOutcome
from app.gateway.protocol import (
    GatewayErrorCode,
    GatewayMessage,
    command_rejected,
    quick_message_received,
)
from app.gateway.quick_message_limits import QuickMessageRateLimiter
from app.gateway.quick_messages import SENDABLE_STATES, QuickMessage, parse_quick_message
from app.gateway.room_service import GameRoomService
from app.modules.game.public import MatchRoster, MatchRosterReader
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)

#: The refusals, each as a wire code, a metric label and a fixed sentence.
#:
#: A table rather than sentences built at the call sites, for the reason
#: `commands._REJECTIONS` is one: §9 forbids the client learning anything
#: about the server's internals, and the only way to guarantee that is for
#: the wire message never to be derived from what actually failed.
#:
#: The sentences are **English and server-authored**, which is the one place
#: this feature carries prose — and it is not a contradiction of §8. A
#: refusal is an error surface a client branches on by `code`; the sentence
#: is a fallback for a client that has no string for the code yet, exactly
#: as `move_rejected` and `command_rejected` already work. The *message* is
#: never prose, and that is the property that matters.
_REFUSALS: Final[dict[QuickMessageOutcome, tuple[GatewayErrorCode, str]]] = {
    QuickMessageOutcome.REJECTED_INVALID: (
        GatewayErrorCode.UNKNOWN_QUICK_MESSAGE,
        "That message is not available.",
    ),
    QuickMessageOutcome.REJECTED_NOT_IN_ROOM: (
        GatewayErrorCode.NOT_IN_ROOM,
        "Join the match room first.",
    ),
    QuickMessageOutcome.REJECTED_NOT_PARTICIPANT: (
        GatewayErrorCode.NOT_A_PARTICIPANT,
        "That match is not yours.",
    ),
    QuickMessageOutcome.REJECTED_TERMINAL: (
        GatewayErrorCode.MATCH_NOT_ACTIVE,
        "That match is not being played.",
    ),
    QuickMessageOutcome.RATE_LIMITED: (
        GatewayErrorCode.RATE_LIMITED,
        "Too many messages. Slow down.",
    ),
    QuickMessageOutcome.INTERNAL: (
        GatewayErrorCode.INTERNAL_ERROR,
        "Something went wrong. Try again.",
    ),
}


class QuickMessageHandler:
    """Handles `game.quick_message.send`."""

    def __init__(
        self,
        *,
        rosters: MatchRosterReader,
        rooms: GameRoomService,
        broadcaster: RoomBroadcaster,
        limiter: QuickMessageRateLimiter,
        metrics: MetricsRecorder,
    ) -> None:
        self._rosters = rosters
        self._rooms = rooms
        self._broadcaster = broadcaster
        self._limiter = limiter
        self._metrics = metrics

    async def handle(
        self,
        message: GatewayMessage,
        *,
        player_id: UUID,
        connection_id: UUID,
        received_at: datetime,
    ) -> GatewayMessage | None:
        """One send. `None` when it was accepted, a refusal otherwise.

        Never raises. A handler that raised would reach the connection
        lifecycle's catch-all and close the socket, which for a player who
        pressed "good luck" is a game interrupted by a courtesy.

        `received_at` is the read loop's receive instant, reused rather than
        re-read from a clock: it is the same authority a move is stamped
        with, so a quick message and the move beside it cannot disagree
        about which happened first.
        """
        if not await self._limiter.allow(connection_id):
            return self._refuse(QuickMessageOutcome.RATE_LIMITED, request_id=message.request_id)

        match_id = _match_id_of(message)
        quick_message = parse_quick_message(message.payload.get("message"))
        if match_id is None or quick_message is None:
            # One answer for "no match named" and "not in the catalogue".
            # Both are a client this server cannot serve, and the payload
            # itself is **never logged** — it is untrusted input, and a log
            # of rejected bodies is the free-text archive this feature
            # exists not to have.
            return self._refuse(QuickMessageOutcome.REJECTED_INVALID, request_id=message.request_id)

        if not await self._rooms.is_attached(
            match_id, player_id=player_id, connection_id=connection_id
        ):
            # Cheaper than the roster read, and this is what keeps a
            # **spectator** out: a viewer is in the audience, never in the
            # room, so they are refused before `game` is asked anything.
            return self._refuse(
                QuickMessageOutcome.REJECTED_NOT_IN_ROOM, request_id=message.request_id
            )

        roster = await self._resolve(match_id)
        if isinstance(roster, QuickMessageOutcome):
            return self._refuse(roster, request_id=message.request_id)

        if not roster.includes(player_id):
            # Unreachable while a room can only be joined by a participant,
            # and checked anyway: this is the authoritative answer, and the
            # room is a cache of a decision made earlier.
            return self._refuse(
                QuickMessageOutcome.REJECTED_NOT_PARTICIPANT, request_id=message.request_id
            )

        if roster.status not in SENDABLE_STATES:
            return self._refuse(
                QuickMessageOutcome.REJECTED_TERMINAL, request_id=message.request_id
            )

        await self._fan_out(roster, sender_id=player_id, message=quick_message, sent_at=received_at)
        self._metrics.increment(QUICK_MESSAGES, labels={"outcome": QuickMessageOutcome.SENT})
        return None

    async def _resolve(self, match_id: UUID) -> MatchRoster | QuickMessageOutcome:
        """The match's roster, or the outcome its absence produces.

        A read failure is `INTERNAL` rather than a refusal that names the
        match, because the two are genuinely different: `not_a_participant`
        tells a client to stop asking, and a database that was briefly
        unreachable is worth retrying.
        """
        try:
            roster = await self._rosters.roster_of(match_id)
        except Exception as exc:  # noqa: BLE001 — every failure becomes a wire code
            logger.error(
                "gateway_quick_message_roster_failed",
                extra={"error": type(exc).__name__},
                exc_info=exc,
            )
            return QuickMessageOutcome.INTERNAL

        if roster is None:
            return QuickMessageOutcome.REJECTED_NOT_PARTICIPANT
        return roster

    async def _fan_out(
        self,
        roster: MatchRoster,
        *,
        sender_id: UUID,
        message: QuickMessage,
        sent_at: datetime,
    ) -> None:
        """Sends the message to the match's two seats. Never raises.

        **Recipients are derived from the roster, never from the frame.**
        That is the whole of §3's "a client cannot target an arbitrary
        recipient" and "cross-match delivery is impossible": the payload has
        no recipient field to read, and the only match this can reach is the
        one whose room the sender was already proven to be in.

        ## This one function is the mute extension point — §7

        Recipient-side suppression, when A64-023.2 adds it, belongs here:
        it is the single place a recipient list exists, and a filter applied
        to `_recipients_of` suppresses a message without touching game
        state, the sender's experience or the fan-out below it. See
        `specs/quick-messages.md` §7 on why an ordinary *mute* is expected
        to be the client's and a *block* the server's.

        Never raises and never buffered. Not buffered for the reason
        `GameCommandHandler._broadcast` gives: `RedisMatchEventBuffer` is
        keyed by the match sequence and nothing here advances the ply, so an
        entry would break the contiguity check a resume depends on. A client
        that was away simply did not hear it (§5).
        """
        frame = quick_message_received(
            match_id=roster.match_id,
            sender_side=_side_of(roster, sender_id),
            message=message.value,
            sent_at=sent_at,
        )

        try:
            report = await self._broadcaster.deliver(
                frame,
                recipients=_recipients_of(roster),
                # No `spectators` argument at all, so `SPECTATOR_SAFE_EVENTS`
                # never even has to withhold this frame. Two independent
                # reasons an audience cannot receive it — the allowlist, and
                # a call site that passes no audience.
            )
        except Exception as exc:  # noqa: BLE001 — a bubble must not close a socket
            logger.warning(
                "gateway_quick_message_delivery_failed", extra={"error": type(exc).__name__}
            )
            return

        # One line per message, at `INFO`, carrying **no message identifier
        # and no player** — §10. What an operator needs from this is whether
        # fan-out is reaching anybody; who said what to whom is not
        # something this platform keeps (ADR-004).
        logger.info(
            "gateway_quick_message_delivered",
            extra={
                "match_id": str(roster.match_id),
                "local": report.local,
                "remote_nodes": report.remote_nodes,
                "failures": report.failures,
            },
        )

    def _refuse(self, outcome: QuickMessageOutcome, *, request_id: str | None) -> GatewayMessage:
        """One refusal, counted and rendered from the fixed table."""
        self._metrics.increment(QUICK_MESSAGES, labels={"outcome": outcome})
        code, reason = _REFUSALS[outcome]
        return command_rejected(code, request_id=request_id, reason=reason)


def _recipients_of(roster: MatchRoster) -> Sequence[UUID]:
    """The two seats, and nobody else.

    Its own function rather than a tuple literal inside `_fan_out`, so the
    suppression seam §7 describes has one obvious place to go and so the
    "recipients come from the roster" property is readable on its own.
    """
    return (roster.light_player_id, roster.dark_player_id)


def _side_of(roster: MatchRoster, player_id: UUID) -> str:
    """Which seat the sender holds.

    A side rather than a player id goes on the wire — see
    `protocol.quick_message_received`. Derived from the roster rather than
    from anything the client sent, which is what makes impersonation
    structurally impossible: there is no field on the frame that could name
    a sender, and this is the only thing that decides one.
    """
    return "light" if player_id == roster.light_player_id else "dark"


def _match_id_of(message: GatewayMessage) -> UUID | None:
    """The frame's match, or `None` if it does not name one readably.

    **No player id and no recipient**, and the frame has no field for
    either (§3). The sender is the socket's redeemed ticket, which is
    structural rather than remembered: there is nothing client-supplied in
    scope to prefer by accident.
    """
    raw = message.payload.get("match_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


__all__ = ["QuickMessageHandler"]
