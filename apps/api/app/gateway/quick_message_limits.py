"""Per-connection quick-message rate limiting — A64-023.1 §6.

The abuse boundary, defined now even though the complete enforcement is
A64-023.3's. What is here is the minimum that stops this shipping as an
obviously spam-capable primitive, and the shape that makes the rest easy to
add.

## Its own budget, not the move budget

`GameCommandHandler` shares `ConnectionMoveLimiter` with the move path, and
the reason is stated there: a client must not be able to dodge the move
limit by alternating moves with draw offers. Quick messages are the
opposite case and take their own budget, because sharing would run the
causation backwards — a player who spams `nice_move` would consume the
allowance their *moves* need, and the punishment for being annoying would
be losing on time. A social channel must never be able to starve the
gameplay one.

The reverse is not a risk worth defending against: a client that alternates
moves with quick messages to evade the move limit gains nothing, because
the move limit is still refusing its moves.

## Two windows, spent atomically

    burst      3 in 10 seconds. What a player pressing three buttons in a
               scramble looks like, and what a loop does not
    sustained  6 in 60 seconds. A whole game's worth of courtesy — "gl", a
               couple of "nice move", "gg", "thanks" — inside one minute

Neither alone is enough. A burst rule alone permits a message every three
seconds forever, which is a player typing at somebody for an hour; a
sustained rule alone permits six in one second and then silence, which is
the flood the recipient actually experiences.

They are spent in **one** `acquire` call rather than two, which is the
contract `RateLimiter` was built for and the reason it takes a sequence:
two sequential calls would charge the burst bucket for a request the
sustained rule then refuses, so the effective burst limit would be lower
than its configuration and nobody could reproduce why. All or nothing.

## What is deliberately not here

**No duplicate suppression.** "The same identifier twice in a row" is a
real rule and it is A64-023.3's — see `specs/quick-messages.md` §7 for
where it goes. At six a minute the repetition it would prevent is already
bounded to something a recipient can ignore, and building a per-connection
last-sent memory for it now would be an anti-abuse subsystem this task is
explicitly told not to build.

**No database.** §6 forbids using writes as the rate limiter, and nothing
here writes: the platform's sliding-window Lua over Redis is the same
mechanism every other limit on this platform uses.
"""

import logging
from typing import Protocol
from uuid import UUID

from app.core.rate_limiting import RateLimiter, RateLimitRule, RateLimitSubject

logger = logging.getLogger(__name__)


class QuickMessageRateLimiter(Protocol):
    """Bounds how fast one connection may send quick messages.

    Its own port rather than reusing `MoveRateLimiter`, even though the
    signature is identical: the two are wired to different budgets, and a
    shared type is one a composition root could satisfy with the wrong
    limiter without anything noticing. The names are the check.
    """

    async def allow(self, connection_id: UUID) -> bool:
        """Whether this connection may send now. **Never raises.**"""
        ...


class ConnectionQuickMessageLimiter:
    """The two windows, over the platform's sliding-window limiter."""

    def __init__(
        self, *, limiter: RateLimiter, burst: RateLimitRule, sustained: RateLimitRule
    ) -> None:
        self._limiter = limiter
        self._burst = burst
        self._sustained = sustained

    async def allow(self, connection_id: UUID) -> bool:
        """Whether this connection may send now.

        Fails **open**, the posture `RATE_LIMIT_FAIL_OPEN` gives every
        other limit on this platform. The trade is different here than on
        the move path and lands in the same place: a limiter outage that
        refused quick messages would silence a courtesy nobody loses a game
        over, and one that allowed them costs an unbounded rate of frames
        that change no state, for the duration of an incident somebody is
        already handling. Neither is worth failing a socket over.
        """
        subject = str(connection_id)
        try:
            decision = await self._limiter.acquire(
                [
                    RateLimitSubject(rule=self._burst, subject=subject),
                    RateLimitSubject(rule=self._sustained, subject=subject),
                ]
            )
        except Exception as exc:  # noqa: BLE001 — a limiter must not fail a frame
            logger.warning(
                "gateway_quick_message_limit_unavailable", extra={"error": type(exc).__name__}
            )
            return True

        return decision.allowed


class UnlimitedQuickMessages:
    """Allows everything.

    Wired by `GATEWAY_QUICK_MESSAGE_RATE_LIMIT_ENABLED=false`, and real
    production code rather than a test double — the same argument
    `UnlimitedMoves` makes. A deployment turning this off has an unbounded
    quick-message rate, which is why the switch exists at all: the
    alternative is somebody commenting out a dependency under pressure and
    forgetting to restore it.
    """

    async def allow(self, connection_id: UUID) -> bool:
        return True


__all__ = [
    "ConnectionQuickMessageLimiter",
    "QuickMessageRateLimiter",
    "UnlimitedQuickMessages",
]
