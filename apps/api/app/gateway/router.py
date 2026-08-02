"""`GET /ws` — the WebSocket handshake. A64-016.1 §1.

Deliberately the thinnest file in this package. It accepts the socket, wraps
it, and hands it to `GatewayConnectionService`; every rule about tickets,
registration, presence and cleanup is one call away and none of it is here.
That is A64-016.1 §8's "the FastAPI WebSocket route should be a thin adapter"
and architecture.md R-7's "the gateway contains no domain logic", and the
measure of both is that this module imports no repository, no store and no
module internals.

## Why the socket is accepted before the ticket is checked

Backwards-looking at first glance: a handshake that fails authentication has
already been upgraded. It is required by the protocol — a WebSocket close
frame carrying a code the client can read only exists *after* the upgrade,
and refusing before it produces an HTTP error that a browser's `WebSocket`
surfaces to the page as an untyped `error` event with no code and no reason.

The cost is bounded and small: the connection is accepted, one frame is sent,
and it is closed with `1008` — all synchronously, with nothing registered and
no presence written. AD-09's concern is that the gateway must not "hold and
account for unauthenticated connections", and this holds one for the duration
of a single `redeem`.

## Why the ticket is a query parameter

Because a browser cannot set headers on a `WebSocket` handshake, which is
AD-09's opening premise and the whole reason the credential is a
seconds-lived single-use ticket rather than an access token. The value lands
in load-balancer and proxy logs; by the time anyone reads those, it has both
expired and been spent.

`Query(...)` with no default, so a handshake with no ticket at all is refused
by FastAPI before this function runs — the cheapest possible answer to the
scanner traffic that is most of what an internet-facing `/ws` receives.

## Why it is mounted at `/ws` and not under `/api/v1`

`API_V1_PREFIX` versions the *HTTP* surface, and this connection is versioned
by its own envelope instead (`PROTOCOL_VERSION`): a socket negotiates once
and then lives for an hour, so a version in the path would pin a long-lived
connection to a number chosen at connect time, which is exactly what the
in-band field exists to avoid.
"""

import logging

from fastapi import APIRouter, Query, WebSocket

from app.gateway.dependencies import GatewayServiceDep
from app.gateway.socket import StarletteGatewaySocket

logger = logging.getLogger(__name__)

gateway_router = APIRouter(tags=["gateway"])


@gateway_router.websocket("/ws")
async def websocket_gateway(
    websocket: WebSocket,
    gateway: GatewayServiceDep,
    ticket: str = Query(
        ...,
        min_length=1,
        max_length=256,
        description=(
            "A single-use WebSocket ticket from `POST /api/v1/auth/ws-ticket`. "
            "Valid for seconds and redeemable once."
        ),
    ),
) -> None:
    """Serves one authenticated realtime connection.

    Three statements, and the middle one is the whole task. `run` never
    raises — which matters more here than on an HTTP route, because a
    WebSocket handler has no exception handler behind it: an escaping
    exception produces a dropped socket and a traceback rather than the
    response envelope every other endpoint on this platform returns.
    """
    await websocket.accept()
    await gateway.run(StarletteGatewaySocket(websocket), ticket=ticket)


__all__ = ["gateway_router"]
