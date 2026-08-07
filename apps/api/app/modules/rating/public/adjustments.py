"""What one finished match did to a player's rating — A64-023 §2.

The narrowest possible published view of `rating_adjustment`: two numbers
and the difference between them. The row behind it holds eleven more —
deviations, volatilities, the opponent's whole triple, the expected and
actual scores, the algorithm version — and **none of that is here**.

## Why the published shape is three integers

The consumer is a result screen saying *"1524 → 1537, +13"*. Everything else
on the row exists to answer *"why"*, which is a rating-history surface
nobody has built and which would be `rating`'s own to serve when somebody
does. Publishing the whole row now would make every future change to the
algorithm's bookkeeping a change to a contract `game` depends on.

`algorithm_version` in particular is deliberately absent: it names an
internal that only means something beside the inputs it applied to.

## Integers, and where the rounding belongs

Ratings are stored as floats because Glicko-2 computes in floats and the
stored value is the one the next calculation reads. A player is shown a
whole number, and rounding it **here** is what stops two consumers rounding
differently — a screen that showed `+13` beside a profile that showed `1537`
after a `1536.5` would be two roundings of one fact.

`delta` is derived from the rounded pair rather than rounded separately, so
`after - before` always equals what the screen shows. Rounding a float
difference independently produces `+12` beside `1524 → 1537` often enough
to be noticed.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RatingChange:
    """One player's rating before and after one match.

    Constructed through `of`, never directly from stored floats — that is
    what makes the rounding rule single-sourced.
    """

    before: int
    after: int

    @property
    def delta(self) -> int:
        """`after - before`.

        A property rather than a stored field, and **the only place this
        subtraction happens on the platform**: a delta computed a second
        time is a delta that can disagree, and one persisted in a column
        would be a third copy that could drift from the pair beside it.
        """
        return self.after - self.before

    @classmethod
    def of(cls, *, before: float, after: float) -> "RatingChange":
        """The stored floats as the published pair. Rounds once."""
        return cls(before=round(before), after=round(after))


class MatchRatingAdjustmentReader(Protocol):
    """What a rated match did to **one** player's rating — A64-023 §2.

    Published so a consumer can show a result without reaching into
    `rating.infrastructure`, and shaped for the one question a result screen
    asks: *for these matches I just listed, what happened to my rating?*

    ## Batch-only, deliberately

    There is no `change_for(player_id, match_id)`. A history page is twenty
    matches, and a per-match method is an invitation to call it in a loop —
    the N+1 §3 forbids, and one that would be invisible in any test with a
    single match. The singular case is a one-element sequence and costs the
    same query.

    ## Scoped to one player by construction

    `player_id` is not a filter applied afterwards; it is half the key
    (`uq_rating_adjustment__player_match`). A caller holding a page of match
    ids cannot use this to read what those matches did to the *opponent* —
    there is no argument that would ask for it.
    """

    async def changes_for(
        self, player_id: UUID, match_ids: Sequence[UUID]
    ) -> Mapping[UUID, RatingChange]:
        """This player's rating change in each of those matches, by match id.

        **A match absent from the result is a match with no adjustment**,
        and that is a real state rather than an error: a casual match never
        produces one, and a rated match produces one only once the outbox
        consumer has processed `game.match_completed`. A caller must
        distinguish the two by whether the match was rated, which it already
        knows — see `MatchHistoryEntry.rated`.

        An empty `match_ids` reads nothing and returns an empty mapping.
        """
        ...


__all__ = ["MatchRatingAdjustmentReader", "RatingChange"]
