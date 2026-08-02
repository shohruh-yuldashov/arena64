"""The wire format — A64-016.1 §6.

Four message types and one envelope. Everything a socket carries goes
through `decode` and `encode`, so there is exactly one place that knows what
a frame looks like, and no code above this module handles a raw `dict`.

## Why an envelope at all, this early

The alternative — a bare JSON object per message type — reads simpler for
four types and gets progressively worse. Three things become impossible
without a common wrapper, and each of them is expensive to add later because
it changes every message already in circulation:

    version      AD-11 multiplexes one socket, so a client that supports a
                 newer protocol has to be able to say so before the server
                 sends it something it cannot parse
    type         a router needs one field to dispatch on that is in a fixed
                 place regardless of what the message is
    request_id   a correlation token echoed back on the response. Without
                 it, a client with two moves in flight cannot tell which
                 acknowledgement is which — and AD-23's optimistic board
                 depends on matching a confirmation to the move it confirms

`request_id` is carried and echoed **now**, before anything needs it,
because the round trip that first needs it (A64-016.2's move submission) will
otherwise have to change the envelope for every existing type at once.

## Why `type` is a closed enum

An open string is a router that dispatches on whatever a client sends, which
means an unknown type is an unhandled branch somewhere rather than a refusal
at the boundary. The enum is also what makes "reject malformed messages
safely" a single check rather than a growing chain of them.

The set is deliberately four. `connection.ready`, `ping`, `pong` and `error`
are the whole of what a connection that carries no game can say — and adding
a live-game type here without the handler behind it is exactly the
"speculative generality" CLAUDE.md §1.7 forbids, which this platform has
declined twice before (`TokenType.ACCESS` alone, `PasswordHasher.hash`
alone).

## Errors carry a code, not a reason

`error` payloads name a member of `GatewayErrorCode`. A free-text reason
would be either useless to a client (which cannot branch on prose) or a
disclosure oracle (which is what `InvalidToken` exists to avoid): a client
learning *why* its ticket was refused learns how to probe the ticket format.
The server knows precisely what happened and says so in its logs.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

#: The protocol this build speaks. A client sending a different version is
#: refused rather than best-guessed — see `decode`.
#:
#: Bumped when an existing message's *shape* changes incompatibly. Adding a
#: type is not a bump: an older client never asks for a type it does not
#: know, and a server that receives one it does not know refuses it.
PROTOCOL_VERSION: Final = 1

#: The envelope's field names, named once so the encoder and the decoder
#: cannot disagree about them. A typo in one of a matched pair is a bug that
#: only shows up against a real client.
_VERSION: Final = "v"
_TYPE: Final = "type"
_REQUEST_ID: Final = "request_id"
_PAYLOAD: Final = "payload"
_CHANNEL: Final = "channel"

#: The longest `request_id` this gateway will echo.
#:
#: It exists because the value is **client-supplied and reflected**: without
#: a bound, a client can make the server allocate and send back whatever it
#: is willing to encode, which turns a correlation token into an
#: amplification primitive. Sixty-four characters is a UUID with room to
#: spare.
MAX_REQUEST_ID_LENGTH: Final = 64


class Channel(StrEnum):
    """Which logical stream a frame belongs to — AD-11, A64-016.2 §4.

    AD-11 puts **one socket per client, multiplexed by channel**, and gives
    two reasons: browsers limit concurrent connections per origin and mobile
    clients pay a battery cost per socket, but more importantly separate
    sockets for moves and chat would make cross-stream ordering undefined —
    a resignation and a chat message sent in that order must arrive in that
    order.

    So the channel is a **field**, not a connection. Three members, each
    owned by a different producer, which is what makes the split worth
    having:

        system       the connection itself — readiness, heartbeat, errors
        matchmaking  queue and pairing notifications (A64-015.5's pending
                     match delivery is the first thing that will use it)
        game         one live match's traffic

    Deliberately not per-match. A channel is a *kind* of traffic; which
    match a frame concerns is in the payload, because a member per live
    match would make this enum unbounded — and an unbounded label is exactly
    what §11 forbids on a metric.

    **Defaults to `system` when a frame omits it**, which is what makes this
    a backwards-compatible addition rather than a protocol bump: an
    A64-016.1 client sends no channel and every frame it sends is a system
    frame, so `PROTOCOL_VERSION` stays at 1.
    """

    SYSTEM = "system"
    MATCHMAKING = "matchmaking"
    GAME = "game"


class MessageType(StrEnum):
    """Everything this build can send or receive.

    Values are dotted and namespaced the way `DomainEvent.event_type` is, so
    the live-game types A64-016.2 adds (`match.move`, `match.state`) sort
    beside each other and a router can dispatch on a prefix.
    """

    CONNECTION_READY = "connection.ready"
    """Server to client, once, immediately after the ticket is redeemed.

    The client's signal that the socket is **authenticated**, not merely
    open. Without it a client cannot distinguish "connected and trusted"
    from "connected and about to be closed because the ticket was spent",
    since both look like an open socket for as long as the round trip takes.
    """

    PING = "ping"
    """Client to server. See `app/gateway/connections.py` on why the client
    drives the heartbeat rather than the server."""

    PONG = "pong"
    """Server to client, in answer to a `ping`. Echoes the `request_id`."""

    ROOM_JOIN = "room.join"
    """Client to server, on the `game` channel — A64-016.2 §5.

    Asks to enter one match's routing scope. Carries `match_id` and
    **nothing else**: the player is the socket's authenticated identity, and
    a client-supplied player id would be a client choosing whose room it
    joins (§7)."""

    ROOM_LEAVE = "room.leave"
    """Client to server. Leaves a room this connection is in. Idempotent —
    leaving one it is not in is not an error."""

    ROOM_JOINED = "room.joined"
    """Server to client, confirming a join, with the room as it now
    stands."""

    ROOM_LEFT = "room.left"
    """Server to client, confirming a leave."""

    ERROR = "error"
    """Server to client. Always carries a `GatewayErrorCode`, never prose."""


class GatewayErrorCode(StrEnum):
    """Why the server refused something.

    A closed set, so a client can branch on it, and deliberately **coarse**:
    `INVALID_TICKET` covers unknown, expired and already-spent because
    distinguishing them is the oracle `InvalidToken` refuses to be.
    """

    INVALID_TICKET = "invalid_ticket"
    """The handshake presented nothing redeemable. The connection closes
    immediately after."""

    MALFORMED_MESSAGE = "malformed_message"
    """The frame could not be decoded: not JSON, not an object, too large,
    an unknown type, or the wrong protocol version. The connection **stays
    open** — see `GatewayConnectionService`."""

    NOT_A_PARTICIPANT = "not_a_participant"
    """The socket asked to join a room for a match it is not in — or one
    that does not exist. **One code for both**, for the reason
    `INVALID_TICKET` covers three: distinguishing them makes live match
    identifiers enumerable by response, which is the same argument
    `MatchAcceptanceUseCase.accept` makes for collapsing them into
    `MatchNotFound`."""

    ROOM_UNAVAILABLE = "room_unavailable"
    """The match exists and this player is in it, but it is not in a state
    that has a room — see `GameRoomService`. Distinct from
    `NOT_A_PARTICIPANT` because it discloses nothing the caller does not
    already know (they are a participant) and because the client's response
    differs: wait, rather than stop asking."""

    INTERNAL_ERROR = "internal_error"
    """Something failed that the client cannot act on. Matches the platform's
    HTTP `ErrorCode.INTERNAL_ERROR` so one vocabulary covers both
    transports."""


class MalformedFrame(Exception):
    """A frame that could not be decoded into a message.

    Carries a `GatewayErrorCode` rather than a message, so the caller sends
    a code and logs the detail. An exception rather than a `None` return
    because the two outcomes of `decode` are genuinely different kinds —
    "here is a message" and "there is no message here" — and a caller that
    forgot to check a nullable return would treat garbage as a `ping`.
    """

    def __init__(self, code: GatewayErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        """For the log, never for the client. See this module's docstring on
        why the wire carries a code alone."""


@dataclass(frozen=True, slots=True)
class GatewayMessage:
    """One frame, decoded.

    Frozen: a message is a fact about something that was received or is
    about to be sent, and a handler that could mutate one in place would be
    a handler whose effect depends on what ran before it.
    """

    type: MessageType
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    channel: Channel = Channel.SYSTEM
    """Which stream this frame belongs to — AD-11.

    Defaulted rather than required, so every A64-016.1 call site that
    constructs a system frame is unchanged and cannot accidentally be
    stamped with somebody else's channel."""

    version: int = PROTOCOL_VERSION

    def to_json(self) -> str:
        """The frame as it goes on the wire.

        `request_id` is omitted rather than sent as `null` when absent, so a
        client's parser sees a field that is either present and meaningful
        or not there at all. `separators` drops the whitespace `json.dumps`
        adds by default — a byte per field per frame, across every socket.
        """
        frame: dict[str, Any] = {
            _VERSION: self.version,
            _TYPE: self.type.value,
            _CHANNEL: self.channel.value,
            _PAYLOAD: self.payload,
        }
        if self.request_id is not None:
            frame[_REQUEST_ID] = self.request_id
        return json.dumps(frame, separators=(",", ":"))


def connection_ready(*, protocol_version: int = PROTOCOL_VERSION) -> GatewayMessage:
    """The first frame a redeemed connection receives.

    Carries the protocol version in the payload as well as the envelope,
    which is not redundancy: the envelope field says what *this frame* is,
    and the payload field is the client's answer to "what may I send" — the
    seam a future client uses to decide whether it can speak to this node
    before it tries.

    It deliberately carries **no identity**. The client already knows who it
    is; echoing the player id back would put an identifier in a frame that
    proves nothing and can be logged by anything in between.
    """
    return GatewayMessage(
        type=MessageType.CONNECTION_READY, payload={"protocol_version": protocol_version}
    )


def pong(*, request_id: str | None) -> GatewayMessage:
    """The answer to a `ping`, correlated back to it."""
    return GatewayMessage(type=MessageType.PONG, request_id=request_id)


def error(
    code: GatewayErrorCode,
    *,
    request_id: str | None = None,
    channel: Channel = Channel.SYSTEM,
) -> GatewayMessage:
    """A refusal the client can branch on.

    Carries the channel the failing frame arrived on, so a client
    multiplexing three streams knows which of its requests was refused —
    an error that always came back on `system` would be an error a `game`
    handler could not attribute.
    """
    return GatewayMessage(
        type=MessageType.ERROR,
        payload={"code": code.value},
        request_id=request_id,
        channel=channel,
    )


def room_joined(
    *, match_id: UUID, participants: Sequence[UUID], both_connected: bool, request_id: str | None
) -> GatewayMessage:
    """Confirmation that this connection is in a match's routing scope.

    Carries the **participants**, which a client already knows — it is one
    of them and `PendingMatchView` named the other — and `both_connected`,
    which it cannot know and is the whole reason to send anything back
    beyond an acknowledgement.

    Carries **no connection ids and no node id** (§3): which sockets the
    other player holds, and which process holds them, is internal topology
    that a browser cannot act on and that maps the fleet for anyone who
    collects it.
    """
    return GatewayMessage(
        type=MessageType.ROOM_JOINED,
        payload={
            "match_id": str(match_id),
            "participants": [str(player_id) for player_id in participants],
            "both_connected": both_connected,
        },
        request_id=request_id,
        channel=Channel.GAME,
    )


def room_left(*, match_id: UUID, request_id: str | None) -> GatewayMessage:
    """Confirmation that this connection has left a room.

    Sent for an idempotent leave as well as a real one — see
    `GameRoomService.leave` on why a client asking to leave a room it is
    not in gets the outcome it asked for rather than an error.
    """
    return GatewayMessage(
        type=MessageType.ROOM_LEFT,
        payload={"match_id": str(match_id)},
        request_id=request_id,
        channel=Channel.GAME,
    )


def decode(raw: str, *, max_bytes: int) -> GatewayMessage:
    """One received frame as a message, or `MalformedFrame`.

    Every rejection is the same kind of event and none of them closes the
    connection — a client that sends one bad frame is far more likely to be
    a version skew or a bug than an attack, and dropping the socket would
    turn a recoverable client defect into a reconnect loop.

    The checks are ordered by cost: length before parsing, because the whole
    point of the bound is to refuse before allocating.
    """
    if len(raw.encode("utf-8")) > max_bytes:
        raise MalformedFrame(GatewayErrorCode.MALFORMED_MESSAGE, "frame exceeds the size limit")

    try:
        frame = json.loads(raw)
    except ValueError as exc:
        raise MalformedFrame(GatewayErrorCode.MALFORMED_MESSAGE, "not valid JSON") from exc

    if not isinstance(frame, dict):
        # A JSON array or a bare scalar parses fine and has no `type`.
        # Checked explicitly so the failure is "not an object" rather than
        # an `AttributeError` three lines later.
        raise MalformedFrame(GatewayErrorCode.MALFORMED_MESSAGE, "frame is not an object")

    if frame.get(_VERSION) != PROTOCOL_VERSION:
        raise MalformedFrame(GatewayErrorCode.MALFORMED_MESSAGE, "unsupported protocol version")

    raw_type = frame.get(_TYPE)
    if not isinstance(raw_type, str):
        raise MalformedFrame(GatewayErrorCode.MALFORMED_MESSAGE, "type is missing or not a string")

    try:
        message_type = MessageType(raw_type)
    except ValueError as exc:
        raise MalformedFrame(GatewayErrorCode.MALFORMED_MESSAGE, "unknown message type") from exc

    payload = frame.get(_PAYLOAD, {})
    if not isinstance(payload, dict):
        raise MalformedFrame(GatewayErrorCode.MALFORMED_MESSAGE, "payload is not an object")

    return GatewayMessage(
        type=message_type,
        payload=payload,
        request_id=_request_id_of(frame),
        channel=_channel_of(frame),
        version=PROTOCOL_VERSION,
    )


def _channel_of(frame: dict[str, Any]) -> Channel:
    """Which stream the client says this frame is on.

    An absent channel is `system`, which is what makes the field a
    backwards-compatible addition — an A64-016.1 client sends none and every
    frame it sends is a system frame.

    An **unknown** one is refused rather than defaulted, and the asymmetry
    is deliberate: absent means "an older client", which is a thing to
    support, while `"chat"` from a build that does not have chat means the
    two ends disagree about what this socket can carry, and silently
    treating it as `system` would deliver it somewhere nobody intended.
    """
    raw = frame.get(_CHANNEL)
    if raw is None:
        return Channel.SYSTEM

    try:
        return Channel(raw)
    except ValueError as exc:
        raise MalformedFrame(GatewayErrorCode.MALFORMED_MESSAGE, "unknown channel") from exc


def _request_id_of(frame: dict[str, Any]) -> str | None:
    """The correlation token, if the client sent a usable one.

    A non-string or an over-long value is **dropped rather than rejected**,
    which is the one place this decoder is lenient and is deliberate: the
    field is optional, the server echoes it as a courtesy, and failing a
    whole frame over a malformed courtesy would refuse work the client
    legitimately asked for. What must not happen is echoing it, which is
    what the bound prevents.
    """
    value = frame.get(_REQUEST_ID)
    if isinstance(value, str) and 0 < len(value) <= MAX_REQUEST_ID_LENGTH:
        return value
    return None


__all__ = [
    "MAX_REQUEST_ID_LENGTH",
    "PROTOCOL_VERSION",
    "Channel",
    "GatewayErrorCode",
    "GatewayMessage",
    "MalformedFrame",
    "MessageType",
    "connection_ready",
    "decode",
    "error",
    "pong",
    "room_joined",
    "room_left",
]
