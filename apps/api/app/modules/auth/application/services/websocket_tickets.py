"""`WebSocketTicketService` — mints and spends AD-09's ticket. A64-016.1.

Twelve lines of logic over two collaborators, and that is the point: the
generation, the hashing and the comparison are `OpaqueTokenService`'s, which
has been the platform's single implementation of DB-24 since A64-011.6 and
which named this as its fourth consumer before it existed. The atomicity is
the store's. What is left here is the *use case* — who may have a ticket,
how long it lasts, and what a redemption returns.

## Why this is not a second authentication mechanism

A64-016.1 forbids one, and it would be easy to build one by accident: a
socket that authenticates itself is a socket that decides what a valid
credential looks like. It does not. `POST /auth/ws-ticket` is an ordinary
authenticated route behind `CurrentUser`, so the *only* thing that ever
verifies a credential on this platform is still `TokenValidator`, and a
ticket is downstream of a successful access-token check rather than
alongside it.

The consequence worth stating: a ticket cannot outlive its own issuance by
more than `GATEWAY_TICKET_TTL_SECONDS`, and it cannot be minted at all
without a live access token. Revoking a session does not reach a ticket
already in flight — but the window is seconds, and a socket already open is
a separate question that belongs to whatever first needs to close one.

## Never raises on redemption

`redeem` returns `None` rather than raising, because at the point it is
called the caller is a WebSocket handshake rather than an HTTP request:
there is no exception handler, no response envelope, and no status code —
the only thing the gateway can do with a failure is close the socket. A
service that raised would put that translation in the transport, which is
where it least belongs.
"""

import logging
from datetime import timedelta
from uuid import UUID

from app.core.clock import Clock
from app.modules.auth.application.ports import WebSocketTicketStore
from app.modules.auth.application.services.opaque_tokens import OpaqueTokenService
from app.modules.auth.domain.tickets import IssuedWebSocketTicket, RedeemedTicket

logger = logging.getLogger(__name__)


class WebSocketTicketService:
    """Issues a ticket to an authenticated player, and spends one on
    connect."""

    def __init__(
        self,
        *,
        store: WebSocketTicketStore,
        tokens: OpaqueTokenService,
        clock: Clock,
        ttl_seconds: int,
    ) -> None:
        self._store = store
        self._tokens = tokens
        self._clock = clock
        self._ttl_seconds = ttl_seconds

    async def issue(
        self, player_id: UUID, *, session_id: UUID | None = None
    ) -> IssuedWebSocketTicket:
        """Mints one ticket for a player who has already been authenticated.

        **Not throttled here.** The route carries the platform's rate limit
        the same way every other `auth` route does, so a client cannot mint
        tickets in a loop; putting a second limiter in the service would be
        two places to get one policy right.

        No previous ticket is invalidated. A client legitimately holds more
        than one — a second tab opening while the first is still connecting
        — and they are each single-use and expire in seconds, so there is
        nothing an accumulated set of them enables. That is the opposite of
        `PasswordResetToken`, where the at-most-one-live rule exists because
        a reset link is a full account takeover with a fifteen-minute life.
        """
        value = self._tokens.generate()
        expires_at = self._clock.now() + timedelta(seconds=self._ttl_seconds)

        await self._store.issue(
            self._tokens.hash(value),
            player_id=player_id,
            session_id=session_id,
            ttl_seconds=self._ttl_seconds,
        )

        # The id and nothing else. The value is a live credential and the
        # digest is the key it is stored under — logging either would put a
        # working ticket in a system with broader read access than the store
        # (caching.md C-6, services.md §8.5).
        logger.debug("websocket_ticket_issued", extra={"user_id": str(player_id)})
        return IssuedWebSocketTicket(value=value, expires_at=expires_at)

    async def redeem(self, value: str) -> RedeemedTicket | None:
        """Spends a presented ticket. `None` if there was nothing to spend.

        One `None` for unknown, expired and already-spent, deliberately
        undistinguished — see `WebSocketTicketStore.redeem`. The gateway
        closes the socket identically in all three cases, and a caller that
        could tell them apart could map the ticket format.

        An **empty or absent** value short-circuits without touching the
        store: a handshake with no ticket at all is the overwhelmingly
        common case for a scanner, and hashing nothing to look it up would
        make that traffic cost a round trip each.
        """
        if not value:
            return None

        return await self._store.redeem(self._tokens.hash(value))


__all__ = ["WebSocketTicketService"]
