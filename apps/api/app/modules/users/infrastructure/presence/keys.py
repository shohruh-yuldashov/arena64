"""The presence keyspace — the contract between whatever writes presence
and whatever reads it.

Infrastructure, not domain: a Redis key is a storage detail in the same way
a column name is, and `users.domain.presence` deliberately knows nothing
about either. It is a *published* storage detail all the same — a gateway
node and an API node have to agree on it byte for byte across separate
deploys — which is why it is one named module rather than a string literal
in the adapter.

## The key

    presence:v1:<player_id>

One key per player, holding the whole record, with a TTL. architecture.md
§755 specifies exactly this shape ("Presence | Keys with TTL | Self-expiring
by nature. A row that must be swept by a cron job is the wrong tool for a
fact that is only true while a socket is open").

**Never logged and never published.** A64-012.7: "never expose Redis keys."
The adapters log `user_id`, which is the same information in the form the
rest of the platform already records it in (services.md §8.5), and no
response schema carries anything from this module.

## Why the version segment

`v1` is not decoration. The one extension this design is known to need is
multiple devices per player, which A64-012.7 excludes and which does not fit
a single key: it wants a key per session with the player's presence derived
from the set of them. That is a different keyspace, not a wider value, so it
arrives as `presence:v2:` written and read alongside `v1` until every node
has rolled — which is only possible if the prefix says which shape a key is.

Without the segment the choices during that deploy are a flag day or a
keyspace where two shapes are indistinguishable.

## Why one string and not a hash

A hash would let a writer touch `last_seen` without rewriting the rest, and
nothing wants to: `PresenceRecorder.record_presence` writes the whole
observation or none of it, because every field comes from the same instant.

What the string buys is that `SET key value PX ttl` is **one command**. A
hash needs `HSET` and `PEXPIRE`, which is either two round trips on the
gateway's hot path or a transaction to close the window in which a key
exists with no expiry — and a presence key that never expires is a player
who is online forever, which is the single worst failure this design can
have.
"""

from typing import Final
from uuid import UUID

#: The keyspace this build reads and writes. Bumped only when the *shape*
#: changes — see this module's docstring on the multi-device migration that
#: is the known reason it would.
KEY_VERSION: Final = "v1"

_KEY_PREFIX: Final = f"presence:{KEY_VERSION}:"

#: The JSON field names inside the stored value. Named constants rather than
#: literals for the reason the key prefix is one: a writer in the gateway
#: and a reader here are separately deployed halves of one contract, and a
#: renamed field is a silent decode failure rather than a compile error.
#:
#: Short, because they are repeated once per online player and the payload is
#: not read by a human — but not *abbreviated*, because they are read by
#: whoever debugs a keyspace at 3am.
FIELD_ONLINE: Final = "online"
FIELD_LAST_SEEN: Final = "last_seen"
FIELD_SESSION_ID: Final = "session_id"
FIELD_DEVICE_TYPE: Final = "device_type"


def presence_key(player_id: UUID) -> str:
    """The key holding this player's presence record.

    Takes a `UUID` rather than a string so a caller cannot pass a username by
    accident — a presence key derived from a handle would move when a player
    renames, and renaming is a flow UP-2 already has planned.
    """
    return f"{_KEY_PREFIX}{player_id}"
