"""Per-connection move rate limiting — A64-016.3 §13.

Move submission is the first expensive per-frame operation on this platform:
a database read, a position load, a legal-move generation and a Redis
compare-and-set. Everything before it — `ping`, `room.join` — is bounded by
what a socket can physically send, and this is not.

## Over the platform limiter, not beside it

`ConnectionMoveLimiter` wraps `core.RateLimiter`, which already owns the
sliding-window Lua, the fail-open posture and the key derivation. A second
implementation would be a second place for "count requests in a window" to
be got right, and the platform has been explicit since A64-011.8 that there
is one.

What is added is the **scope**. `RateLimitScope` gained `CONNECTION`: a
player with two tabs is two clients, and a limit shared between them would
let one tab's misbehaviour throttle the other's game — which on a live board
is losing on time for somebody else's bug.

## Why a boolean rather than the decision

A socket has no headers. `RateLimitDecision` carries `remaining`,
`reset_after` and `retry_after_seconds` because an HTTP route renders them
into `X-RateLimit-*`, and a handler that unpacked all three to discard two
would be machinery pretending to be a policy. The client is told
`rate_limited` and slows down.

## The connection is not closed

§13: "do not close the connection for one normal rate-limit violation". A
player whose client is bursting is far more likely to have a UI bug than to
be attacking, and dropping the socket turns that into a reconnect loop
against the whole fleet. The heartbeat timeout and the frame-size bound
remain the mechanisms that do close a connection.
"""

import logging
from uuid import UUID

from app.core.rate_limiting import RateLimiter, RateLimitRule, RateLimitSubject

logger = logging.getLogger(__name__)


class ConnectionMoveLimiter:
    """`MoveRateLimiter` over the platform's sliding-window limiter."""

    def __init__(self, *, limiter: RateLimiter, rule: RateLimitRule) -> None:
        self._limiter = limiter
        self._rule = rule

    async def allow(self, connection_id: UUID) -> bool:
        """Whether this connection may submit a move now.

        **Never raises**, and fails *open* — the same posture
        `RATE_LIMIT_FAIL_OPEN` gives every other limit on the platform, and
        the right one here for a specific reason: a limiter outage that
        refused moves would stop every game in progress, while one that
        allowed them costs an unbounded submission rate for the duration of
        an incident that is already being handled.
        """
        try:
            decision = await self._limiter.acquire(
                [RateLimitSubject(rule=self._rule, subject=str(connection_id))]
            )
        except Exception as exc:  # noqa: BLE001 — a limiter must not fail a move
            logger.warning("gateway_move_limit_unavailable", extra={"error": type(exc).__name__})
            return True

        return decision.allowed


class UnlimitedMoves:
    """Allows everything.

    Wired by `GATEWAY_MOVE_RATE_LIMIT_ENABLED=false`, and real production
    code rather than a test double — the same argument `NoPresenceProvider`
    makes. A deployment turning this off has an unbounded move rate, which
    is why the switch exists at all: somebody commenting out a dependency
    under pressure and forgetting to restore it is the alternative.
    """

    async def allow(self, connection_id: UUID) -> bool:
        return True


__all__ = ["ConnectionMoveLimiter", "UnlimitedMoves"]
