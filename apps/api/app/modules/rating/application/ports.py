"""What `rating`'s use cases need from the world — AD-06.

One port, and it is deliberately narrow: a rating is read by key and written
by an adjustment, and there is no third thing this module does.

## Why `load` returns an aggregate for a player who has no row

SPEC-RATING §7.5 writes no row until the first rated match, so "absent" and
"1500/350/0.06, zero games" are the same state seen from two sides. A
repository that returned `None` would push that equivalence onto every
caller, and the first caller to treat absence as an error would refuse to
rate somebody's first game.

## Why saving is one method that takes both

`save(rating, adjustment)` rather than two calls, because they are one fact:
PR-1 makes the adjustment the exactly-once record *of* the rating change, and
a caller that could write one without the other is a caller that can produce
a rating nobody can explain — or an explanation for a rating that never
moved.

The unique violation is the mechanism, not an error: see
`AdjustmentAlreadyApplied`.
"""

from typing import Protocol
from uuid import UUID

from app.core.exceptions import DomainError
from app.modules.rating.domain.keys import RatingKey
from app.modules.rating.domain.player_rating import PlayerRating, RatingAdjustment


class AdjustmentAlreadyApplied(DomainError):
    """This match has already moved this player's rating — PR-1.

    Raised when `uq_rating_adjustment__player_match` refuses the insert,
    which is the **normal** outcome of a relay redelivering
    `game.match_completed` rather than an exceptional one. The handler
    treats it as success: the work was done by whoever won the race.

    A distinct type rather than letting `IntegrityError` escape, so the
    caller branches on a domain fact instead of on a driver's exception —
    and so a *different* constraint violation is not silently swallowed as
    a duplicate.
    """


class PlayerRatingRepository(Protocol):
    """The aggregate's storage. Reads by key, writes a rating and its
    adjustment together."""

    async def load(self, player_id: UUID, *, key: RatingKey) -> PlayerRating:
        """This player's rating in this key, or the unrated starting state.

        Never `None` — see this module's docstring. Locks nothing: two
        matches for one player completing at once are serialised by the
        unique index rather than by a row lock, because the second is a
        legitimate second game rather than a conflict to arbitrate.
        """
        ...

    async def save(self, rating: PlayerRating, adjustment: RatingAdjustment) -> None:
        """Persists the new rating and the record of how it got there.

        One call, one transaction — see this module's docstring. Raises
        `AdjustmentAlreadyApplied` if this match has already been applied to
        this player.
        """
        ...


__all__ = ["AdjustmentAlreadyApplied", "PlayerRatingRepository"]
