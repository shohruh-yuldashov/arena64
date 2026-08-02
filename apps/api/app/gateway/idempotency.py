"""Move-submission idempotency, on `request_id` — A64-016.3 §7.

## Two mechanisms, two failures, and why they are not the same one

This is the point worth getting right, because they look interchangeable and
are not:

    request_id dedupe   the **client retried**. Same connection, same
                        intent, sent twice because the first answer was
                        lost. The correct outcome is the *first* answer,
                        replayed.
    ply compare-and-set the **opponent moved first**. Two different
                        intents against the same state. The correct outcome
                        is one applied and one refused.

A CAS alone would let a retry apply a second, different move — the retry
carries the same path, but by then the ply has advanced, so it fails as
`StaleMatchState` rather than returning the original success. A dedupe alone
would let two genuinely concurrent moves both apply. §6 and §7 ask for both,
and this is the second one.

## Scope: `(connection_id, request_id)`

§7 asks for the scope to be explicit. It is the **connection**, not the
player and not the match:

- A `request_id` is a value the *client* chooses, and two tabs choosing
  `"1"` are two independent clients. Keying on the player would make one
  tab's retry return the other tab's answer.
- Keying on the match would do the same across a reconnect, where the client
  legitimately starts its counter again.

The connection is the exact boundary within which a client controls its own
`request_id` sequence, which is why it is the scope.

## Bounded, and why the TTL is short

§7 requires a bounded TTL and forbids "a permanent unbounded request cache".
The window a retry actually needs is a client timeout — seconds — so the
default is a minute. Longer would keep answers for retries nobody will send;
shorter would let a slow client's retry through as a fresh submission, which
the CAS then refuses as stale. Both are safe; the second is confusing.

A frame with **no** `request_id` is not deduplicated at all, and that is the
honest behaviour: there is nothing to key on, and inventing one would be
inventing the second correlation identifier §7 forbids.

## What is stored, and what is deliberately not

The **encoded response frame**, so a retry replays exactly what the first
attempt sent — including the rejection, because §7 says "returns the prior
accepted or rejected outcome". Storing a decision the caller re-renders would
let the two answers drift.

Rejections are stored too. A client retrying an illegal move should be told
it is illegal a second time, not have the move re-run against a position that
may have changed underneath it.
"""

import logging
from typing import Final
from uuid import UUID

from redis.asyncio import Redis

from app.gateway.protocol import GatewayMessage, MalformedFrame, decode

logger = logging.getLogger(__name__)

#: Bumped when the *stored value* changes — caching.md C-2. It holds an
#: encoded frame, so a protocol version bump that changed the envelope is
#: the reason it would.
KEY_VERSION: Final = "v1"

_KEY_PREFIX: Final = f"gwmove:{KEY_VERSION}:"

#: The ceiling on a *stored* frame, which this build wrote itself.
#:
#: Generous relative to `GATEWAY_MAX_FRAME_BYTES`, which bounds what a
#: client may send: a server frame carries a fingerprint whose size grows
#: with the pieces on the board, and refusing to replay one because it was
#: larger than an inbound limit would be applying the wrong bound to the
#: wrong direction.
_MAX_STORED_FRAME_BYTES: Final = 64 * 1024


class RedisMoveIdempotencyStore:
    """Remembers one answer per `(connection, request_id)`.

    On the **`cache`** role. Losing an entry costs a duplicate submission
    being processed as a fresh one — which the ply compare-and-set then
    refuses as stale — so the failure mode is a confusing message rather
    than a double-applied move. That is exactly the expendable posture
    `cache` is configured for, and it is why this is not on `live` beside
    the position it protects.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(connection_id: UUID, request_id: str) -> str:
        return f"{_KEY_PREFIX}{connection_id}:{request_id}"

    async def replay(self, connection_id: UUID, request_id: str) -> GatewayMessage | None:
        """The answer this request already produced, or `None`.

        `None` for a request never seen and for one whose entry has
        lapsed — indistinguishable, and correctly so: both mean "process
        it", and the CAS is what stops a lapsed retry applying twice.

        A stored frame that will not decode is also `None`, and is logged:
        it means a build wrote a frame this one cannot read, and
        reprocessing is strictly better than replaying something the client
        would reject.

        **Never raises.** A dedupe store that failed would fail a move, and
        the fallback — process it fresh — is safe because the ply
        compare-and-set is the mechanism that actually prevents a double
        application.
        """
        try:
            stored = await self._redis.get(self._key(connection_id, request_id))
        except Exception as exc:  # noqa: BLE001 — a cache read must not fail a move
            logger.warning(
                "gateway_move_idempotency_read_failed",
                extra={"error": type(exc).__name__},
            )
            return None

        if stored is None:
            return None

        raw = stored.decode("utf-8") if isinstance(stored, bytes) else stored
        try:
            return decode(raw, max_bytes=_MAX_STORED_FRAME_BYTES)
        except MalformedFrame:
            logger.warning("gateway_move_idempotency_frame_malformed")
            return None

    async def remember(
        self, connection_id: UUID, request_id: str, *, frame: GatewayMessage, ttl_seconds: int
    ) -> None:
        """Records the answer, with its expiry in the same command.

        `SET ... EX` rather than `SET` then `EXPIRE` — caching.md C-4. A
        crash between the two would leave an answer that never expires,
        and an immortal idempotency entry is a client that can never
        resubmit that `request_id` again.

        **Never raises.** It runs after the move has already been applied
        and acknowledged, so a failure here costs a retry being reprocessed
        (which the CAS handles) rather than anything the player can see —
        and raising would turn a cache write into a failed move.
        """
        try:
            await self._redis.set(
                self._key(connection_id, request_id), frame.to_json(), ex=ttl_seconds
            )
        except Exception as exc:  # noqa: BLE001 — a cache write must not fail a move
            logger.warning(
                "gateway_move_idempotency_write_failed",
                extra={"error": type(exc).__name__},
            )


__all__ = ["KEY_VERSION", "RedisMoveIdempotencyStore"]
