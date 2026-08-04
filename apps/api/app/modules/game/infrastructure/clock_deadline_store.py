"""`RedisClockDeadlineStore` — AD-21's deadlines. A64-016.5 §5.

AD-21: *"The clock is adjudicated by a worker against Redis, not by
in-process timers."* Its reasoning, restated because it is the whole design:
an `asyncio` timer per match lives on one gateway node, and if that node is
deployed, crashes or is rescheduled, every timer it held silently disappears
and those matches never flag — they hang forever. A deadline in a Redis
sorted set is owned by no node and is adjudicated by whichever worker is
healthy.

## The keyspace

    clock:v1:deadlines  ->  sorted set
                            member = "<match_id>|<ply>|<side>"
                            score  = deadline, epoch milliseconds

One key for the whole fleet rather than one per match, because the question
a worker asks is *"which matches have expired"* — a range query over scores,
which is what a sorted set answers in `O(log N)` and what a key-per-match
would answer with a `SCAN`.

## Why the ply is in the member

It is the **version** (§5). A deadline written for ply 7 is superseded the
moment ply 8 lands, and packing the version into the member makes that
structural: the new deadline is a *different member*, so writing it cannot
silently overwrite a check somebody else is mid-way through.

Superseding is therefore remove-then-add, in one transaction, and a worker
that claimed the ply-7 member holds a token that says exactly which position
it is entitled to adjudicate. §6's "reject stale worker adjudication" is that
token being compared against the match row, not a lock.

The side is packed in for the same reason: a worker adjudicating a flag must
know **whose** without loading a position, and deriving it from ply parity at
two places is two places to get it wrong.

## Claiming is atomic, and that is a Lua script

`ZRANGEBYSCORE` then `ZREM` in two round trips lets two workers both read the
same member before either removes it, and both would then adjudicate the same
match. One script does range-and-remove together, so exactly one worker
receives each member — the same argument `GETDEL` makes for the WebSocket
ticket.

Claiming **removes**. A worker that dies after claiming loses the deadline
rather than blocking it, and the match is left with no deadline until its
next move writes one — which is the safer failure: a game that stops flagging
is a bug somebody reports, where a deadline nothing can claim is a game that
hangs and looks identical to a quiet one.

## Which Redis role

`live`, beside the live position. A deadline is not reconstructible from
anything cheap — the match row has the clock, so it *could* be rebuilt, but
the rebuild is a scan of every active match — and `cache` is configured to
evict, which would make the eviction policy a way for a game to stop
flagging.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from redis.asyncio import Redis

from app.modules.engine import PlayerSide
from app.modules.game.application.ports import ClaimedDeadline
from app.modules.game.domain.clock import MILLISECONDS_PER_SECOND

logger = logging.getLogger(__name__)

#: Bumped when the *member shape* changes — caching.md C-2. The known reason
#: it would is a multi-stage time control, whose deadline is per stage.
KEY_VERSION: Final = "v1"

#: One key, fleet-wide. See this module's docstring on why not per match.
DEADLINE_KEY: Final = f"clock:{KEY_VERSION}:deadlines"

#: Separates the three parts of a member. The same character
#: `gwconn:v2:` uses, and `resolve_node_id` already refuses a node name
#: containing it — one separator on the platform rather than three.
_SEPARATOR: Final = "|"

#: Claim expired deadlines and remove them, in one command.
#:
#: `KEYS[1]` the deadline key. `ARGV`: the instant to claim up to, and how
#: many. Returns the claimed members.
#:
#: Range-then-remove in two round trips lets two workers claim the same
#: match. This is the atomic form, and it is why §6's "safe with multiple
#: workers" is a property of the store rather than of the worker.
_CLAIM = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
if #due > 0 then
    redis.call('ZREM', KEYS[1], unpack(due))
end
return due
"""

#: Replace a match's deadline, whatever version it was written for.
#:
#: `KEYS[1]` the deadline key. `ARGV`: the match id, the new member, the new
#: score, and the separator.
#:
#: Removes every member for this match before adding the new one, so a
#: superseded deadline cannot survive as a second entry. Scanning the set is
#: acceptable because it is bounded by *concurrent games*, and a match has
#: at most one live deadline — the loop exists to be certain rather than to
#: be fast.
_SUPERSEDE = """
local prefix = ARGV[1] .. ARGV[4]
local existing = redis.call('ZRANGE', KEYS[1], 0, -1)
for i = 1, #existing do
    if string.sub(existing[i], 1, string.len(prefix)) == prefix then
        redis.call('ZREM', KEYS[1], existing[i])
    end
end
if ARGV[2] ~= '' then
    redis.call('ZADD', KEYS[1], ARGV[3], ARGV[2])
end
return 1
"""


class RedisClockDeadlineStore:
    """`ClockDeadlineStore` over the `live` role."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._claim = redis.register_script(_CLAIM)
        self._supersede = redis.register_script(_SUPERSEDE)

    @staticmethod
    def _member(match_id: UUID, ply_number: int, side: PlayerSide) -> str:
        return f"{match_id}{_SEPARATOR}{ply_number}{_SEPARATOR}{side.value}"

    @staticmethod
    def _score(deadline: datetime) -> float:
        """The deadline as epoch milliseconds.

        Milliseconds rather than seconds throughout the clock — see
        `game.domain.clock` on why a second-resolution flag would round a
        bullet player's remaining time to zero.
        """
        return deadline.timestamp() * MILLISECONDS_PER_SECOND

    async def schedule(
        self, match_id: UUID, *, ply_number: int, side: PlayerSide, deadline: datetime
    ) -> None:
        """Writes this match's deadline, replacing whatever it had.

        Replace rather than add, in one script, so a match cannot end up
        with two live deadlines — which would flag it twice, once for a
        position it had already left.
        """
        await self._supersede(
            keys=[DEADLINE_KEY],
            args=[
                str(match_id),
                self._member(match_id, ply_number, side),
                str(self._score(deadline)),
                _SEPARATOR,
            ],
        )

    async def cancel(self, match_id: UUID) -> None:
        """Removes this match's deadline — §5, on match completion.

        Idempotent: a match with no deadline is the ordinary state of every
        untimed and every finished game, and removing nothing is not a
        failure.
        """
        await self._supersede(keys=[DEADLINE_KEY], args=[str(match_id), "", "0", _SEPARATOR])

    async def claim_expired(self, *, now: datetime, limit: int) -> Sequence[ClaimedDeadline]:
        """Up to `limit` deadlines that have passed, claimed exclusively.

        Bounded (CLAUDE.md §10.5) and atomic — see this module's docstring
        on why claiming is one script.

        A member this build cannot parse is dropped and logged rather than
        raising: it was written by a different build during a rolling
        deploy, and one unadjudicated match is better than a worker that
        stops adjudicating every match.
        """
        claimed = await self._claim(keys=[DEADLINE_KEY], args=[str(self._score(now)), str(limit)])

        decoded = (self._decode(member) for member in claimed)
        return tuple(deadline for deadline in decoded if deadline is not None)

    async def pending(self) -> int:
        """How many deadlines are live. For an operator and for a test —
        nothing in the flow branches on it."""
        return int(await self._redis.zcard(DEADLINE_KEY))

    @staticmethod
    def _decode(member: bytes | str) -> ClaimedDeadline | None:
        text = member.decode("utf-8") if isinstance(member, bytes) else member
        parts = text.split(_SEPARATOR)
        if len(parts) != 3:
            logger.warning("clock_deadline_malformed")
            return None

        try:
            return ClaimedDeadline(
                match_id=UUID(parts[0]),
                ply_number=int(parts[1]),
                side=PlayerSide(parts[2]),
            )
        except ValueError:
            logger.warning("clock_deadline_malformed")
            return None


__all__ = ["DEADLINE_KEY", "KEY_VERSION", "RedisClockDeadlineStore"]
