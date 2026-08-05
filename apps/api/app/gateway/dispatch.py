"""The message dispatch table — A64-016.3 §1.

A64-016.1 shipped a two-branch match and said the table "arrives with
A64-016.2, when there is something to dispatch to". A64-016.2 added two more
branches and said it again: "a table earns its place when a handler needs its
own collaborators, which is A64-016.3's move submission." That is now true —
`MoveSubmissionHandler` holds six — so this is the table.

## What it is, and what it deliberately is not

A `dict` from `MessageType` to a callable, built once at construction. §1 is
explicit about the shape:

    no dynamic imports              nothing here imports at call time
    no reflection-based discovery   no decorator registry, no metaclass, no
                                    scanning a module for handlers
    deterministic                   a dict literal over a closed enum; the
                                    same frame reaches the same handler on
                                    every process, every time
    handlers get what they need     `ping` takes the connection; the move
                                    handler takes its own collaborators and
                                    the frame

**Not a generic event framework**, which §1 forbids and which is the failure
mode this shape is chosen against: no middleware chain, no priorities, no
wildcard subscriptions, no handler that can register another. Adding a
message type is one entry and one method.

## Why the table is built per connection rather than per process

The handlers close over per-connection identity — which player, which
connection — so a process-wide table would need every handler to take those
as arguments and every call site to pass them. Building the table once per
connection costs one dict of four entries at accept time and makes every
handler signature the same two words.

That is also what makes `_completeness` checkable: the table is constructed
in one place, so "every client-sendable type has a handler" is an assertion
about a literal rather than about whatever happened to register.

## Unknown types

A type only the *server* sends — `connection.ready`, `pong`, `error`,
`game.move.accepted` — is decodable and is not something a client may send.
It gets `malformed_message` rather than silence, because silence leaves a
confused client waiting for an answer that is never coming.

A type this build does not know at all never reaches here: `protocol.decode`
refuses it, since `MessageType` is a closed enum.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from uuid import UUID

from app.gateway.protocol import (
    Channel,
    GatewayErrorCode,
    GatewayMessage,
    MessageType,
    error,
)

logger = logging.getLogger(__name__)

#: What a handler is: one decoded frame in, one frame back to the sender.
#:
#: Every handler returns a message rather than sending one, so the read loop
#: owns the socket and a handler cannot write to it out of turn — which is
#: what keeps ordering a property of one place (AD-11's cross-stream
#: guarantee) rather than of every handler behaving.
#:
#: `None` means "nothing to send back", which is `room.leave`'s answer when
#: the client asked for something that is already true and has been told so
#: — and is the seam a future fire-and-forget message would use.
MessageHandler = Callable[[GatewayMessage], Awaitable[GatewayMessage | None]]


class MessageDispatch:
    """Routes one decoded frame to the handler that owns its type.

    Holds a mapping and nothing else. The handlers are bound closures built
    by the connection lifecycle, so this class knows what a `MessageType` is
    and knows nothing about rooms, moves or presence.
    """

    def __init__(self, handlers: Mapping[MessageType, MessageHandler]) -> None:
        self._handlers = dict(handlers)

    @property
    def routes(self) -> frozenset[MessageType]:
        """Which types this table can route.

        Published so the invariant that matters can be asserted: every
        `CLIENT_SENDABLE` type must have an entry, or a frame the decoder
        admits is one nothing handles — and a handler wired into the
        composition root is one nothing can reach. Neither failure raises
        anywhere, which is why it is worth a check rather than a comment
        (A64-020.5C-pre §15).
        """
        return frozenset(self._handlers)

    async def dispatch(self, message: GatewayMessage, *, player_id: UUID) -> GatewayMessage | None:
        """The answer to one frame, or `None` if there is none.

        A type with no handler is refused with `malformed_message` on **the
        channel it arrived on**, so a client multiplexing three streams can
        attribute the refusal — see `protocol.error`.
        """
        handler = self._handlers.get(message.type)
        if handler is None:
            logger.debug(
                "gateway_frame_unexpected",
                extra={"user_id": str(player_id), "message_type": message.type.value},
            )
            return error(
                GatewayErrorCode.MALFORMED_MESSAGE,
                request_id=message.request_id,
                channel=message.channel,
            )

        return await handler(message)

    def handles(self, message_type: MessageType) -> bool:
        """Whether a type has a handler. For a test asserting the table is
        complete; nothing in the lifecycle branches on it."""
        return message_type in self._handlers


#: Every type a **client** may send. The table must cover exactly these.
#:
#: Declared beside the dispatcher rather than on `MessageType`, because it
#: is a statement about *this tier's* protocol direction rather than about
#: what a message is — and `MessageType` is shared with the encoder, which
#: has no opinion about direction.
CLIENT_SENDABLE: frozenset[MessageType] = frozenset(
    {
        MessageType.PING,
        MessageType.ROOM_JOIN,
        MessageType.ROOM_LEAVE,
        MessageType.MOVE_SUBMIT,
        MessageType.RESUME,
        MessageType.RESIGN,
        MessageType.DRAW_OFFER,
        MessageType.DRAW_ACCEPT,
        MessageType.DRAW_DECLINE,
        MessageType.SPECTATOR_JOIN,
        MessageType.SPECTATOR_LEAVE,
    }
)

#: Which channel each client-sendable type belongs on.
#:
#: Not enforced — a `ping` on the `game` channel is answered — and the
#: reason it is not is worth stating: the channel is a *routing* label for
#: the client's benefit, and refusing a frame whose label is merely
#: unconventional would break a client for a cosmetic mismatch. It is
#: documented here so `docs/01-architecture/websocket.md` §8.2 and the code
#: cannot disagree about what a well-behaved client sends.
CLIENT_CHANNELS: Mapping[MessageType, Channel] = {
    MessageType.PING: Channel.SYSTEM,
    MessageType.ROOM_JOIN: Channel.GAME,
    MessageType.ROOM_LEAVE: Channel.GAME,
    MessageType.MOVE_SUBMIT: Channel.GAME,
    MessageType.RESUME: Channel.GAME,
    MessageType.RESIGN: Channel.GAME,
    MessageType.DRAW_OFFER: Channel.GAME,
    MessageType.DRAW_ACCEPT: Channel.GAME,
    MessageType.DRAW_DECLINE: Channel.GAME,
    MessageType.SPECTATOR_JOIN: Channel.GAME,
    MessageType.SPECTATOR_LEAVE: Channel.GAME,
}


__all__ = ["CLIENT_CHANNELS", "CLIENT_SENDABLE", "MessageDispatch", "MessageHandler"]
