"""The `friends:v1:` keyspace — caching.md §3.4, reserved by A64-013.1 and
written for the first time by A64-013.6.

Infrastructure, not domain: a Redis key is a storage detail in the same way
a column name is. It is a *published* storage detail all the same — every
API node reads and invalidates it — which is why it is one named module
rather than string literals in an adapter.

## The two entries, and why only these two

caching.md C-1 and C-3 require a namespace to have a documented owner and a
complete invalidation rule **before** the first key. A64-013.5 made both
knowable, which is why this is the release that writes them:

    friends:v1:friends:<player_id>   the player's live friend ids
    friends:v1:blocked:<player_id>   everyone they cannot interact with,
                                     in either direction

Their invalidation triggers are exhaustive and there are exactly four: a
request accepted, a friendship removed, a player blocked, a player
unblocked. Every one is a method on a service in this module, so there is no
writer anywhere that could change the graph without invalidating — which is
the property caching.md asks for, and the reason nothing else is cached yet.

## Why the whole set rather than the query

`SocialGraphReader.friend_ids_among` takes a *page* of candidate ids, so
caching its result would mean a key per distinct page: unbounded keys, each
invalidated by the same four events, and a hit rate near zero. The cached
value is the player's whole friend set and the intersection happens in
Python — one key per player, and a hit on every page.

## Why a version segment

`v1` is not decoration. The extension this design is known to need is a
*shape* change — a set of ids becoming a sorted set scored by interaction
recency, say — and two shapes must coexist while a fleet rolls. Without the
prefix the choices are a flag day or an ambiguous keyspace.

## Never logged

caching.md C-6. The adapters log `player_id`, which is the same fact in the
form every other log line already carries it in — and never the *contents*,
which are a social graph.
"""

from typing import Final
from uuid import UUID

#: The keyspace this build reads and writes. Bumped only when the *shape*
#: changes — see this module's docstring.
KEY_VERSION: Final = "v1"

_PREFIX: Final = f"friends:{KEY_VERSION}:"


def friend_ids_key(player_id: UUID) -> str:
    """Where this player's live friend ids are cached."""
    return f"{_PREFIX}friends:{player_id}"


def blocked_ids_key(player_id: UUID) -> str:
    """Where this player's block set — both directions — is cached."""
    return f"{_PREFIX}blocked:{player_id}"


def keys_for(player_id: UUID) -> tuple[str, ...]:
    """Every key holding anything about this player.

    What invalidation deletes. A tuple rather than two call sites, so a
    third entry added later is invalidated by every existing trigger without
    any of them being edited — which is the failure caching.md C-1 is really
    about: not an undocumented key, but a documented key somebody forgot to
    expire.
    """
    return (friend_ids_key(player_id), blocked_ids_key(player_id))
