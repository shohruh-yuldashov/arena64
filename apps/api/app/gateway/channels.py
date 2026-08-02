"""Channel multiplexing over one socket — AD-11, A64-016.2 §4.

AD-11: **one socket per client, multiplexed by channel.** §4 restates the
constraint as a prohibition — "do not create a second WebSocket connection
for gameplay" — and as a design note: "a channel-aware socket is another
implementation of the same seam, not a separate service architecture."

So there is no new port here. `ChannelSocket` satisfies `GatewaySocket`,
which is what makes it substitutable everywhere the lifecycle already passes
one, and `MultiplexedSocket` is the thing that hands them out.

## What a channel-bound view actually buys

    socket.on(Channel.GAME)  ->  a GatewaySocket whose sends are game frames

Three things, none of which is available if a collaborator is handed the raw
socket and told to stamp its own channel:

1. **A handler cannot write on somebody else's stream.** A64-016.3's move
   handler will hold `on(Channel.GAME)` and has no way to emit a `system`
   frame — not because it is told not to, but because the object it holds
   cannot.
2. **The stamp cannot be forgotten.** A frame built without a channel
   defaults to `system` (see `protocol.Channel`), which is the *right*
   default for the connection's own traffic and exactly the wrong one for a
   game frame. Forgetting is silent and delivers to the wrong stream.
3. **One writer, so ordering is preserved.** Every view shares the
   underlying socket, which is what AD-11's cross-stream ordering guarantee
   requires — a resignation and a chat message sent in that order arrive in
   that order because they went down one pipe.

## Why `receive` is deliberately not demultiplexed

A channel view returns **every** frame the peer sent, not only the ones on
its channel. That looks like a gap and is the correct shape for one reader:
the connection has a single read loop (`GatewayConnectionService._read_loop`),
and a per-channel `receive` would mean either several loops competing for
one transport — where a frame delivered to the wrong queue is lost — or a
demultiplexer holding per-channel buffers, which is unbounded memory a slow
consumer fills.

The read loop reads once and dispatches on `message.channel`. That is one
place, it cannot drop a frame, and it is what §5 means by keeping the
existing branching.

## Why an explicit override rather than mutating

`ChannelSocket.send` re-stamps with `dataclasses.replace`, because
`GatewayMessage` is frozen and a caller may legitimately hold the same
message object while sending it twice. Re-stamping is also what makes the
view's guarantee true of messages the caller built *before* it had the view.
"""

from dataclasses import replace

from app.gateway.ports import GatewaySocket
from app.gateway.protocol import Channel, GatewayMessage


class ChannelSocket:
    """A `GatewaySocket` whose outbound frames all carry one channel.

    Holds the underlying socket rather than copying anything from it, so a
    view is a few bytes and `close` on any view closes *the* connection —
    which is right: there is one socket, and a channel is a label on its
    traffic rather than a resource of its own.
    """

    def __init__(self, socket: GatewaySocket, channel: Channel) -> None:
        self._socket = socket
        self._channel = channel

    @property
    def channel(self) -> Channel:
        """Which stream this view writes on. For a log line and a test —
        nothing branches on it, because the point of the view is that
        nothing has to."""
        return self._channel

    async def send(self, message: GatewayMessage) -> None:
        """Writes one frame, stamped with this view's channel.

        Overrides whatever the message carried rather than checking it. A
        view whose contract were "the caller must already have stamped it
        correctly" would be a comment, not a type — and the one call site
        that forgets is the one that matters.
        """
        await self._socket.send(replace(message, channel=self._channel))

    async def receive(self) -> str:
        """Delegates. **Not filtered** — see this module's docstring on why
        demultiplexing on the read side is the wrong shape."""
        return await self._socket.receive()

    async def close(self, *, code: int, reason: str) -> None:
        """Closes the underlying connection. There is only one."""
        await self._socket.close(code=code, reason=reason)


class MultiplexedSocket:
    """One connection, addressable per channel.

    Itself a `GatewaySocket` — sending through it directly is a `system`
    frame, which is what the connection's own traffic (`connection.ready`,
    `pong`, transport errors) is. So the lifecycle can hold this object and
    behave exactly as it did in A64-016.1, and a collaborator that needs a
    stream asks for one.

    **Views are cached per channel**, not because building one is expensive
    — it is two attribute assignments — but because identity is the cheapest
    way for a test to assert that two callers on the same channel are
    writing down the same pipe rather than through two wrappers that merely
    behave alike.
    """

    def __init__(self, socket: GatewaySocket) -> None:
        self._socket = socket
        self._views: dict[Channel, ChannelSocket] = {}

    def on(self, channel: Channel) -> ChannelSocket:
        """This connection, as seen by one channel's traffic."""
        view = self._views.get(channel)
        if view is None:
            view = ChannelSocket(self._socket, channel)
            self._views[channel] = view
        return view

    async def send(self, message: GatewayMessage) -> None:
        """Writes one frame **as the message already describes it**.

        Unlike a channel view, this does not re-stamp: the caller here is
        the connection itself, which builds `system` frames by default and
        occasionally builds a `game` frame on a handler's behalf — a
        `room.joined` is produced by the room service and written by the
        lifecycle. Overriding here would undo the channel the producer
        deliberately set.
        """
        await self._socket.send(message)

    async def receive(self) -> str:
        return await self._socket.receive()

    async def close(self, *, code: int, reason: str) -> None:
        await self._socket.close(code=code, reason=reason)


__all__ = ["ChannelSocket", "MultiplexedSocket"]
