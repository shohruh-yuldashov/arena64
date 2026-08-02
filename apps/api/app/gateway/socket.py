"""`StarletteGatewaySocket` — the only file that knows what a WebSocket is.

A64-016.1 §8 asks for framework-independent ports and a thin FastAPI
adapter. This is the adapter, and its entire job is to translate two
vocabularies:

    Starlette                       ->  `GatewaySocket`
    WebSocketDisconnect                 ConnectionClosed
    RuntimeError after disconnect       ConnectionClosed
    receive_text() -> str               receive() -> str
    send_text(str)                      send(GatewayMessage)

## Why `RuntimeError` is caught

Starlette raises `WebSocketDisconnect` from `receive_*` when the peer goes,
but a `send_*` after that point raises a bare `RuntimeError` — the connection
state machine has moved to `DISCONNECTED` and refuses the write. Both mean
the same thing to a caller, and leaving the second uncaught would mean the
lifecycle's `finally` (which sends a close frame) can raise while handling a
disconnect it already knows about.

Catching `RuntimeError` broadly is normally the wrong shape, and it is
narrowed here by *where* it is: the only statement inside the guard is one
Starlette call, so the only `RuntimeError` reachable is Starlette's.

## `close` never raises, deliberately

It runs from a `finally` on every path, including one already handling a
failure. An exception there would replace the original error with a less
useful one and skip nothing useful — the socket is being abandoned either
way, and the operating system closes it when the request scope ends.
"""

import logging

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from app.gateway.ports import ConnectionClosed
from app.gateway.protocol import GatewayMessage

logger = logging.getLogger(__name__)


class StarletteGatewaySocket:
    """`GatewaySocket` over a Starlette `WebSocket`.

    Holds the socket and nothing else. Every piece of state a connection has
    — its identity, its id, when it opened — belongs to the lifecycle, so
    this stays a translation layer with no memory.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send(self, message: GatewayMessage) -> None:
        """Writes one frame as text.

        Text rather than binary: the protocol is JSON, browsers hand a text
        frame to `onmessage` as a string without a decode step, and a binary
        frame carrying UTF-8 JSON would be a second encoding for the same
        bytes that some client eventually reads the other way.
        """
        try:
            await self._websocket.send_text(message.to_json())
        except (WebSocketDisconnect, RuntimeError) as exc:
            raise ConnectionClosed from exc

    async def receive(self) -> str:
        """Waits for one text frame.

        A **binary** frame arriving here raises `KeyError` inside Starlette's
        `receive_text`, which would reach the lifecycle as a server error and
        close the connection with `1011`. Translated to `ConnectionClosed`
        instead: a client sending binary on a text protocol is a client this
        build cannot talk to, and closing normally is the honest outcome
        rather than reporting an internal failure the server did not have.
        """
        try:
            return await self._websocket.receive_text()
        except (WebSocketDisconnect, KeyError, RuntimeError) as exc:
            raise ConnectionClosed from exc

    async def close(self, *, code: int, reason: str) -> None:
        """Closes the socket. Never raises — see this module's docstring."""
        if self._websocket.client_state is WebSocketState.DISCONNECTED:
            # Already gone. Starlette would raise on a second close, and
            # this path is reached whenever the peer hung up first, which is
            # the ordinary ending rather than an unusual one.
            return

        try:
            await self._websocket.close(code=code, reason=reason)
        except (WebSocketDisconnect, RuntimeError) as exc:
            logger.debug("gateway_close_ignored", extra={"error": type(exc).__name__})


__all__ = ["StarletteGatewaySocket"]
