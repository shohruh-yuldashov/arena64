"""The leaderboard — a **derived read**, not a second copy. A64-017.4.

    LeaderboardEntry   one row: who, what they rate, how sure we are
    LeaderboardPage    a page and the cursor that continues it
    LeaderboardReader  the one question this surface answers

## Why there is no projection table and no Redis copy

The task asks for a projection updated on every `rating.updated`. What is
built instead is a query over `rating.player_rating` itself, and the reason
is the task's own constraint — *"do not duplicate the source of truth"*.

A leaderboard is `ORDER BY rating DESC LIMIT n` over a relation that already
exists, is already indexed for exactly that order, and is written by the one
transaction that moves a rating. A second table fed by an event would be:

    at best   identical to the source, and one more thing to keep correct
    at worst  behind it, by however long the relay took
    always    a rebuild path, a consistency question, and a second answer to
              "what does this player rate"

The derived read has none of those. It is not *immediately* consistent with
the rating write — it is **the same rows**, so there is no window at all,
which is the strongest form of what the requirement asks for.

`caching.md` C-5 points the same way: *"a key is never the sole record of
anything competitive"*. A Redis leaderboard would either be that, or a
duplicate — and duplicates of A-4 data are how ladders stop reconciling.

**When this stops being right** is a measurement, not a guess: a page is one
index scan today. If the relation grows to where that scan is the bottleneck,
the first move is a covering index, and the second is a Redis sorted set fed
by `rating.updated` — at which point the ownership, invalidation and rebuild
questions get answered with a number behind them. Recorded in
`specs/leaderboard.md` rather than pre-built.

## Ordering is total, and that is a correctness property

    rating DESC, deviation ASC, player_id ASC

Three keys, because two are not enough to be deterministic and a
non-deterministic order breaks **pagination** rather than merely looking
untidy: a cursor that resumes after a tie can skip a player or show them
twice. `player_id` is the tiebreaker of last resort and is unique, so the
order is total — no two rows can compare equal.

Deviation ascending is second on purpose: between two players on the same
rating, the one the platform is *more sure about* ranks higher. The
alternative — ordering by games played — would rank a grinder above a
stronger player, which is a different product decision nobody made.

## Cursor, not offset

Keyset pagination: the cursor carries the last row's three ordering values
and the next page is "strictly after this tuple". `OFFSET` re-scans and
skips, so it costs more the deeper a reader goes and it *shifts* when a
rating moves between pages — a player can be seen twice or missed entirely
on a live ladder, which is exactly what a leaderboard cannot do.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.rating.domain.keys import RatingKey


@dataclass(frozen=True, slots=True)
class LeaderboardCursor:
    """Where a page ended — the three ordering values of its last row.

    The whole ordering tuple rather than a rank or an offset, because those
    describe a *position in a result* and this describes a *row*: the row
    keeps its identity when ratings around it move, and a position does not.
    """

    rating: float
    deviation: float
    player_id: UUID


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """One player's standing in one key.

    No handle, no avatar, no country. Those are `profiles`' and are composed
    by whoever renders the page — a leaderboard entry that carried them
    would make `rating` depend on a module it has no business knowing about,
    and would make every ranking read a join.
    """

    player_id: UUID
    rating: float
    deviation: float
    games_played: int

    is_provisional: bool
    """PR-6's mark. Provisional players are **shown**, and shown as
    provisional — §6 forbids hiding them and forbids a minimum-games
    threshold. A ladder that hid its newcomers would be a ladder nobody new
    can see themselves on."""


@dataclass(frozen=True, slots=True)
class LeaderboardPage:
    """A page of standings, and how to ask for the next one."""

    entries: Sequence[LeaderboardEntry]

    next_cursor: LeaderboardCursor | None
    """`None` when this was the last page.

    Derived from whether a further row exists rather than from the page
    being short, so a page that happens to be exactly `limit` long and is
    also the last one does not send a reader back for an empty one.
    """


class LeaderboardReader(Protocol):
    """Standings for one `RatingKey`. **Read-only** — §8.

    There is deliberately no method that writes, rebuilds or invalidates
    anything: the leaderboard is a query, so there is nothing to rebuild,
    and a surface that offered one would imply a second copy exists.
    """

    async def page(
        self, key: RatingKey, *, after: LeaderboardCursor | None = None, limit: int = 50
    ) -> LeaderboardPage:
        """One page of `key`'s ladder, highest first.

        `after` continues a previous page; `None` starts at the top. The
        limit is bounded by the implementation — §10.5 makes every list
        endpoint paginate, and an unbounded page is an outage waiting for a
        popular key.
        """
        ...


__all__ = [
    "LeaderboardCursor",
    "LeaderboardEntry",
    "LeaderboardPage",
    "LeaderboardReader",
]
