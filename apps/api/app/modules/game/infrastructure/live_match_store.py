"""`RedisLiveMatchStore` — the in-flight position. AD-18, A64-016.3 §6.

## The keyspace

    game:live:v1:<match_id>  ->  hash { ply: "<int>", position: "<json>" }

Registered in `caching.md` §3 (C-1), versioned (C-2), expiring (C-3) via
`PEXPIRE` inside the script that writes it.

architecture.md AD-18 assigns exactly this: "Live position lives in Redis.
Moves are appended durably to PostgreSQL." **The second half is not built.**
Until the durable move log exists, a Redis primary failure loses an in-flight
game with no replay path — which is the mitigation AD-19 depends on, and is
therefore the headline gap of A64-016.3 rather than an implementation detail.
It is acceptable only because no rated game is played yet. See
`docs/01-architecture/websocket.md` §16.

## Why the `live` role and not `cache`

The one keyspace on this tier that does **not** go on `cache`. AD-03 groups
by hostile interaction, and `cache` is configured to evict — which is correct
for presence, for the connection registry and for rooms, because every one of
those is reconstructible by a reconnect. A live position is not
reconstructible by anything that exists today, so putting it on an instance
whose eviction policy may delete it would make the eviction policy a way to
lose a game.

`live` is the instance architecture.md §956 names for exactly this ("Live
match position | Hash per match").

## Why a Lua script and not `WATCH`/`MULTI`

Both give compare-and-set. `WATCH` gives it by *aborting* on conflict, which
means the caller retries — and a retry loop on the move path is a loop whose
worst case is a player's move taking unbounded time under contention.

The script decides in one round trip and returns whether it wrote. The caller
gets a `bool`, turns `False` into `StaleMatchState`, and never loops. It is
also the shape `RedisRateLimiter` already uses, so the platform has one
answer to "conditional multi-key write" rather than two.

## Why `expected_ply = 0` also accepts an absent key

Lazy seeding — see `LiveMoveService._seeded`. Two nodes both finding no state
and both applying the first move must resolve to one winner, and the script
does: the first `HSET`s the key, and the second's condition (`ply == 0`) no
longer holds because the stored ply is now `1`.

Without that clause the first move of every game would be unwritable.
"""

import json
import logging
from typing import Any, Final
from uuid import UUID

from redis.asyncio import Redis

from app.modules.engine.serialization import (
    position_from_primitive,
    position_to_primitive,
)
from app.modules.game.application.ports import LiveMatchState

logger = logging.getLogger(__name__)

#: Bumped when the *stored shape* changes — caching.md C-2. The known reason
#: it would is the durable move log arriving, which would let this hold a
#: sequence number that indexes into it rather than a whole position.
KEY_VERSION: Final = "v1"

_KEY_PREFIX: Final = f"game:live:{KEY_VERSION}:"

_PLY_FIELD: Final = "ply"
_POSITION_FIELD: Final = "position"

#: Compare-and-set on the ply, with the expiry set in the same call.
#:
#: `KEYS[1]` the match key. `ARGV`: expected ply, new ply, position JSON,
#: TTL in milliseconds.
#:
#: Returns `1` when it wrote and `0` when it did not. Deliberately not an
#: error on the losing path: a failed CAS is an ordinary outcome of two
#: players moving at once, and raising would make the caller distinguish it
#: from a Redis failure by parsing a message.
_ADVANCE = """
local stored = redis.call('HGET', KEYS[1], 'ply')
local expected = ARGV[1]

-- An absent key counts as ply 0, which is what makes lazy seeding safe:
-- the first mover writes, and a concurrent second mover finds ply 1 and
-- loses. See this module's docstring.
if stored == false then
    stored = '0'
end

if stored ~= expected then
    return 0
end

redis.call('HSET', KEYS[1], 'ply', ARGV[2], 'position', ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return 1
"""


class RedisLiveMatchStore:
    """`LiveMatchStore` over the `live` role."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._advance = redis.register_script(_ADVANCE)

    @staticmethod
    def _key(match_id: UUID) -> str:
        return f"{_KEY_PREFIX}{match_id}"

    async def load(self, match_id: UUID) -> LiveMatchState | None:
        """The current live state, or `None`.

        One `HGETALL` rather than two `HGET`s, because the ply and the
        position must come from the same read — a caller that saw ply 4 and
        the position after move 5 would apply a move to the wrong board and
        the CAS would happily accept it.
        """
        stored = await self._redis.hgetall(self._key(match_id))  # type: ignore[misc]
        if not stored:
            return None

        return self._decode(stored)

    async def advance(
        self, match_id: UUID, *, state: LiveMatchState, expected_ply: int, ttl_seconds: int
    ) -> bool:
        """Writes `state` only if the stored ply is still `expected_ply`."""
        wrote = await self._advance(
            keys=[self._key(match_id)],
            args=[
                str(expected_ply),
                str(state.ply),
                json.dumps(position_to_primitive(state.position), separators=(",", ":")),
                str(ttl_seconds * 1000),
            ],
        )
        return bool(wrote)

    @staticmethod
    def _decode(stored: dict[Any, Any]) -> LiveMatchState | None:
        """One stored hash as a state, or `None` if it cannot be read.

        Tolerant rather than raising, for the reason every decoder on this
        platform is: a value written by a different build during a rolling
        deploy must not raise inside a move submission.

        `None` here means the caller falls back to the seeded opening
        position — which for a *corrupt* record is the wrong game, so it is
        logged at `ERROR` rather than `WARNING`. That is the one decode on
        this platform whose failure is not cosmetic, and it is another
        reason the durable move log matters.
        """
        raw = {_as_text(key): _as_text(value) for key, value in stored.items()}
        try:
            return LiveMatchState(
                position=position_from_primitive(json.loads(raw[_POSITION_FIELD])),
                ply=int(raw[_PLY_FIELD]),
            )
        except (KeyError, ValueError, TypeError):
            logger.error("live_match_state_malformed")
            return None


def _as_text(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


__all__ = ["KEY_VERSION", "RedisLiveMatchStore"]
