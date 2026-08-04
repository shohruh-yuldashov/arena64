"""`RedisMatchEventBuffer` — the recent past, for reconnecting clients.
A64-016.6 §3.

## The keyspace

    gwevent:v1:<match_id>  ->  sorted set
                               member = "<sequence>|<frame>"
                               score  = sequence

Registered in `caching.md` §3 (C-1), versioned (C-2), expiring (C-3).

## Why a sorted set scored by sequence rather than a stream

A stream is the obvious shape and is wrong for the one operation this exists
for: *"everything after sequence N"*. A Redis stream is keyed by its own
entry ids, so answering that means either remembering an entry id per client
— state per connection, in a store whose whole point is that connections are
disposable — or scanning from the beginning.

A sorted set scored by the match's own sequence answers it in one
`ZRANGEBYSCORE`, and the score is a number the client already has, because it
is the ply the server told it about.

## Bounded twice, and both bounds are needed

    ZREMRANGEBYRANK   the newest `GATEWAY_EVENT_BUFFER_LENGTH` entries
    EXPIRE            `GATEWAY_EVENT_BUFFER_TTL_SECONDS` on the key

The rank cap bounds a *long* game: a hundred-ply match must not accumulate a
hundred frames per spectator-visible event forever. The key TTL bounds a
*finished* one: a match nobody returns to leaves a capped-but-permanent key,
and one key per match ever played is unbounded in history rather than in
size.

## Why this is not the recovery record

§3 is explicit: *"Do not use PostgreSQL as the hot reconnect buffer. The
durable move log remains the permanent recovery record."* Both halves matter.
This is a **cache of recent frames** — losing it costs a full snapshot, which
is the fallback §6 already requires — and the move log is what a match is
actually reconstructed from.

So a gap in this buffer is not a data-loss incident. It is the signal to send
a snapshot instead, which is why `since` reports whether it could prove
continuity rather than returning what it happened to have.

## Idempotent appends

`ZADD` on an existing member is a no-op, and the member carries the sequence,
so appending the same event twice leaves one entry. That matters because the
fan-out is at-least-once: a frame redelivered by the bus must not appear
twice in a reconnecting client's replay.

A *different* frame for the same sequence would be two members with one
score, which cannot happen — the sequence is the ply and a ply has one move —
and if it ever did, `since` returns both in score order and the client
applies the one it already has as a no-op.
"""

import logging
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

#: Bumped when the *member shape* changes — caching.md C-2.
KEY_VERSION: Final = "v1"

_KEY_PREFIX: Final = f"gwevent:{KEY_VERSION}:"

#: Separates the sequence from the frame. The same character every other
#: gateway keyspace uses, and the one `resolve_node_id` already refuses in a
#: node name — one separator on this platform rather than four.
_SEPARATOR: Final = "|"

#: Append an event, trim the buffer, and refresh its expiry.
#:
#: `KEYS[1]` the buffer key. `ARGV`: the member, its score, how many entries
#: to keep, and the TTL in milliseconds.
#:
#: One script rather than four commands, because the trim must not observe a
#: buffer that is mid-append: a reader between the `ZADD` and the
#: `ZREMRANGEBYRANK` would see one more entry than the bound allows, which is
#: harmless, and a *crash* between them leaves a buffer that never trims,
#: which is not.
_APPEND = """
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
local excess = redis.call('ZCARD', KEYS[1]) - tonumber(ARGV[3])
if excess > 0 then
    redis.call('ZREMRANGEBYRANK', KEYS[1], 0, excess - 1)
end
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return 1
"""


@dataclass(frozen=True, slots=True)
class BufferedEvents:
    """What the buffer could offer a client resuming from a sequence.

    Two fields, and the second is the whole point. `frames` is what to send;
    `is_contiguous` is whether sending them is *safe* — a buffer that has
    trimmed past the client's position can still return frames, and they
    would be a silent partial recovery, which §6 forbids.

    So the caller branches on `is_contiguous` rather than on `frames` being
    non-empty, and the empty-and-contiguous case is the common one: a client
    that missed nothing.
    """

    frames: tuple[str, ...]
    is_contiguous: bool
    """Whether every sequence between the client's and the newest is
    present. `False` means send a snapshot."""


class RedisMatchEventBuffer:
    """`MatchEventBuffer` over the `cache` role.

    `cache`, not `live`: losing it costs a full snapshot rather than a game,
    which is exactly the derived-and-expendable posture that instance is
    configured for — and the same reason the room membership and the
    connection registry live there.
    """

    def __init__(self, redis: Redis, *, max_events: int, ttl_seconds: int) -> None:
        self._redis = redis
        self._max_events = max_events
        self._ttl_seconds = ttl_seconds
        self._append = redis.register_script(_APPEND)

    @staticmethod
    def _key(match_id: UUID) -> str:
        return f"{_KEY_PREFIX}{match_id}"

    async def append(self, match_id: UUID, *, sequence: int, frame: str) -> None:
        """Records one event. Idempotent on the sequence.

        **Never raises.** It runs after the move it describes is already
        committed, so a buffer that could not be written costs a full
        snapshot on the next reconnect — which is the fallback that already
        exists — and raising would turn a cache write into a failed move.
        """
        try:
            await self._append(
                keys=[self._key(match_id)],
                args=[
                    f"{sequence}{_SEPARATOR}{frame}",
                    str(sequence),
                    str(self._max_events),
                    str(self._ttl_seconds * 1000),
                ],
            )
        except Exception as exc:  # noqa: BLE001 — a buffer must not fail a move
            logger.warning(
                "gateway_event_buffer_write_failed",
                extra={"match_id": str(match_id), "error": type(exc).__name__},
            )

    async def since(self, match_id: UUID, *, sequence: int) -> BufferedEvents:
        """Every buffered event after `sequence`, and whether that is all of
        them.

        Continuity is proven by the **oldest** entry, not by the count: the
        buffer covers a client if it still holds the event immediately after
        the one they last saw. If the oldest entry is newer than that, the
        buffer has trimmed past them and no amount of what it does hold
        would give them a complete picture.

        A read failure reports `is_contiguous=False` rather than raising,
        which degrades to a snapshot — the safe direction, because the
        alternative is a client told it is up to date when it is not.
        """
        try:
            raw = await self._redis.zrangebyscore(
                self._key(match_id), min=f"({sequence}", max="+inf"
            )
            oldest = await self._redis.zrange(self._key(match_id), 0, 0, withscores=True)
        except Exception as exc:  # noqa: BLE001 — degrade to a snapshot
            logger.warning(
                "gateway_event_buffer_read_failed",
                extra={"match_id": str(match_id), "error": type(exc).__name__},
            )
            return BufferedEvents(frames=(), is_contiguous=False)

        frames = tuple(frame for frame in (_frame_of(entry) for entry in raw) if frame)

        if not oldest:
            # An empty buffer proves nothing about continuity: it is
            # indistinguishable from one that expired. A client that has
            # seen everything gets a snapshot it does not strictly need,
            # which is the cost of never guessing.
            return BufferedEvents(frames=(), is_contiguous=False)

        earliest = int(oldest[0][1])
        return BufferedEvents(frames=frames, is_contiguous=earliest <= sequence + 1)

    async def length(self, match_id: UUID) -> int:
        """How many events are buffered. For an operator and a test."""
        return int(await self._redis.zcard(self._key(match_id)))


def _frame_of(entry: bytes | str) -> str | None:
    """The frame half of a member, or `None` if it cannot be read.

    `partition` rather than `split`, because a frame is JSON and contains
    the separator: only the first occurrence delimits the sequence.
    """
    text = entry.decode("utf-8") if isinstance(entry, bytes) else entry
    _sequence, separator, frame = text.partition(_SEPARATOR)
    if not separator:
        logger.warning("gateway_event_buffer_member_malformed")
        return None
    return frame


__all__ = ["KEY_VERSION", "BufferedEvents", "RedisMatchEventBuffer"]
