"""`RedisWebSocketTicketStore` — where AD-09's ticket waits. A64-016.1.

## The keyspace

    wsticket:v1:<hex digest>   ->   "<player_id>:<session_id or ->"

One key per ticket, holding the identity the ticket proves, with a TTL equal
to the ticket's lifetime. Registered in `caching.md` §3 alongside `presence:`
and `rl:` (C-1), versioned (C-2), and expiring (C-3).

**The key is the digest, not the ticket.** A read of this keyspace — a
`MONITOR` session, a memory dump, a support engineer with `redis-cli` —
yields SHA-256 digests, and a digest cannot be presented on a socket. That
is DB-24's whole argument applied to a store that is deliberately less
protected than PostgreSQL.

## Why redemption is `GETDEL` and not `GET` then `DEL`

AD-09 requires single use, and the two-command form does not provide it: two
gateway nodes can both `GET` before either `DEL`s, and both then believe they
hold a valid ticket for the same player. That is not a theoretical race — a
client that opens a second tab while the first is still connecting produces
exactly this traffic.

`GETDEL` is one command, so Redis resolves it: exactly one caller receives
the value and every other receives nil. The single-use guarantee is therefore
a property of the storage engine rather than of a check that the second
caller would also pass. A Lua script would work equally well and is what this
would become if redemption ever needed to read a second key.

## `SET ... EX` in one command — C-4

The write sets the value and the expiry together. `SET` followed by `EXPIRE`
is a crash away from a ticket that never dies, and an immortal ticket is
precisely the thing AD-09's short life exists to prevent.

## Which Redis role, and why not `live`

`cache`. Losing a ticket costs the client one round trip — it asks for
another and connects — so it has exactly the derived, expendable, evictable
posture that instance is configured for. `live` holds match positions, and a
reconnect storm is a burst of ticket writes; AD-03's own worked example is
that such a burst must not compete with games in progress.

Eviction under memory pressure is therefore acceptable here **and only
because a ticket is re-mintable**. Nothing else in this keyspace would be.

## Failure posture — the exception to C-7

Every other Redis workload on this platform degrades on failure. This one
**propagates**: a ticket that cannot be stored is a ticket that cannot be
redeemed, and returning success would hand the client a credential the
platform has already forgotten. A redemption that cannot reach Redis is a
connection that must be refused, because the alternative — treating an
unreachable store as "no such ticket" — is already what happens, and
treating it as "valid" would be an authentication bypass.
"""

import logging
from typing import Final
from uuid import UUID

from redis.asyncio import Redis

from app.modules.auth.domain.tickets import RedeemedTicket

logger = logging.getLogger(__name__)

#: Bumped only when the *value shape* changes — caching.md C-2. The known
#: reason it would is a ticket that carries more than an identity, which
#: `IssuedWebSocketTicket` argues against.
KEY_VERSION: Final = "v1"

_KEY_PREFIX: Final = f"wsticket:{KEY_VERSION}:"

#: Separates the two identifiers in the stored value. A colon rather than
#: JSON because the value is two UUIDs and a sentinel: JSON would cost an
#: encode and a decode per socket opened to express a fixed pair.
_FIELD_SEPARATOR: Final = ":"

#: What stands in for a session the ticket was not bound to. A literal
#: rather than an empty segment, so a malformed value with a trailing
#: separator cannot decode as "no session" — it fails to parse instead.
_NO_SESSION: Final = "-"


class RedisWebSocketTicketStore:
    """`WebSocketTicketStore` over the `cache` role."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(digest: bytes) -> str:
        """The digest as hex, which is what makes the key printable.

        Hex rather than the raw bytes because a key containing arbitrary
        bytes is one that a `redis-cli` session, a slow-log line or a
        metrics exporter renders differently in three places. The digest is
        already the full 256 bits either way.
        """
        return f"{_KEY_PREFIX}{digest.hex()}"

    async def issue(
        self, digest: bytes, *, player_id: UUID, session_id: UUID | None, ttl_seconds: int
    ) -> None:
        """Stores one ticket. The value and the expiry in one command."""
        session = str(session_id) if session_id is not None else _NO_SESSION
        await self._redis.set(
            self._key(digest),
            f"{player_id}{_FIELD_SEPARATOR}{session}",
            ex=ttl_seconds,
        )

    async def redeem(self, digest: bytes) -> RedeemedTicket | None:
        """Spends a ticket atomically. `None` if there was nothing there."""
        raw = await self._redis.getdel(self._key(digest))
        if raw is None:
            return None

        return self._decode(raw)

    @staticmethod
    def _decode(raw: bytes | str) -> RedeemedTicket | None:
        """Reads a stored value, or `None` if it cannot be read.

        Tolerant rather than raising, for the reason `RedisPresenceProvider`
        decodes tolerantly: a value this build cannot parse was written by
        a different build during a rolling deploy, and the correct outcome
        is one refused connection rather than an exception on a handshake
        that has no error path. It is logged at `WARNING` because it should
        never happen and would otherwise be invisible.
        """
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        player, _, session = text.partition(_FIELD_SEPARATOR)

        try:
            player_id = UUID(player)
            session_id = None if session == _NO_SESSION else UUID(session)
        except ValueError:
            # No key, no digest, no value — a log line here must not
            # reconstruct the credential it is complaining about (C-6).
            logger.warning("websocket_ticket_malformed")
            return None

        return RedeemedTicket(player_id=player_id, session_id=session_id)


__all__ = ["KEY_VERSION", "RedisWebSocketTicketStore"]
