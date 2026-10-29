"""A player's rating over time — A64-032.

`adjustments.py` anticipated this: the rest of a `rating_adjustment` row
"exists to answer *why*, which is a rating-history surface nobody has built
and which would be `rating`'s own to serve when somebody does". This is the
smallest version of it — the *what*, ordered by time — and still not the
why: no deviations, no opponent triple, no algorithm version.

## History is a query, not a store

SPEC-RATING §7.7: "the entity is the adjustment; the history is the query."
Nothing new is written to produce this. Every point is one immutable
adjustment, read through `ix_rating_adjustment__player_history`, the index
that was built for exactly this read and had no reader until now.

## One key at a time

A rating is of a `(variant, speed_class)` key, and a blitz rating and a
classical one are two different lines rather than one line that jumps. The
reader answers for one key, so a chart cannot accidentally join them.

## Bounded, newest kept

`limit` caps the points and the reader keeps the **most recent** ones,
returned oldest first so they read left to right. `has_earlier` says
whether older adjustments exist beyond the cap, so a client can say "the
last 50 games" rather than imply the line starts at the player's first.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.rating.domain.keys import RatingKey
from app.modules.rating.public.adjustments import RatingChange


@dataclass(frozen=True, slots=True)
class RatingHistoryPoint:
    """One rated match: when it was applied and what it did."""

    match_id: UUID
    applied_at: datetime
    change: RatingChange


@dataclass(frozen=True, slots=True)
class RatingHistory:
    points: Sequence[RatingHistoryPoint]
    """Oldest first. Empty for a key the player has never been rated in."""

    has_earlier: bool
    """Older adjustments exist beyond the requested limit."""


class RatingHistoryReader(Protocol):
    """This player's adjustments in one key, most recent `limit` of them.

    **One player, by construction.** There is no argument that names a
    second player and no batch form: the one consumer is a player reading
    their own line, and the route takes the id from the token.
    """

    async def history_for(self, player_id: UUID, *, key: RatingKey, limit: int) -> RatingHistory:
        """The most recent `limit` adjustments, returned oldest first."""
        ...


__all__ = ["RatingHistory", "RatingHistoryPoint", "RatingHistoryReader"]
