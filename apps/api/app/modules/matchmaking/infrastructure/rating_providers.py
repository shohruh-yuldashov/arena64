"""`application.ports.RatingSnapshotProvider` over `rating.public`.

A64-017.2 replaced the constant this file used to return. Its docstring
predicted the change exactly:

> On the day `rating` ships, its published reader satisfies this port,
> `matchmaking.presentation.dependencies` names it instead of this class,
> and no use case, no aggregate and no test changes.

That is what happened. `QueueService` is untouched, `QueueTicket` is
untouched, and QT-2's rule — a ticket carries the rating it was entered
with, not a reference to a live one — is unchanged; only the number is real
now.

## Why this adapter still exists, now that both sides speak `RatingKey`

A64-020.5A-pre widened the port to `(variant, speed class)`, which is
`RatingKey`'s own two components — so the translation this class performs is
no longer a *vocabulary* one. What is left is a **capability** one, and it
is the reason the adapter is kept rather than handing `RatingReader`
straight to `QueueService`:

`RatingReader` is `rating`'s published reader and may grow methods. This
port has one, and a queue service holding it can read exactly one number for
exactly one player. The narrowing is the value; the type assembly is
incidental.

## The variant is no longer a constant here

It was, and the docstring said what would replace it: "when a second variant
ships, the port's argument widens to carry one". It widened for a different
reason — the speed class — and the variant came with it, so this class no
longer has an opinion about which game is being rated. Both halves of the
key now arrive from the pool and the ticket the player actually chose.

## Casual games are rated *reads*, not rated *writes*

A `casual` ticket still gets a real rating, because pairing a casual game by
skill is what makes it a game rather than a coin toss. What `casual` decides
is whether the *result* moves anything, and that is `rated` on the match
(SPEC-RATING §9) — a decision `game` and `rating` make, not this one. Since
A64-020.5A-pre the port does not even accept a `QueueType`, so this class
could not act on the distinction if it wanted to.
"""

import logging
from uuid import UUID

from app.modules.game.public import ProductVariant
from app.modules.rating.public import RatingKey, RatingReader, RatingSnapshot, SpeedClass

logger = logging.getLogger(__name__)


class PublishedRatingProvider:
    """Reads a player's rating through `rating`'s published surface.

    Holds a `RatingReader` and nothing else, so it cannot move a rating,
    read an adjustment, or reach `rating`'s persistence — R-4's one-way
    chain as a constructor argument rather than a rule to remember.
    """

    def __init__(self, ratings: RatingReader) -> None:
        self._ratings = ratings

    async def rating_for(
        self, player_id: UUID, *, variant: ProductVariant, speed_class: SpeedClass
    ) -> RatingSnapshot:
        """The player's triple in the key this pool rates in."""
        return await self._ratings.rating_for(
            player_id, key=RatingKey(variant=variant, speed_class=speed_class)
        )


__all__ = ["PublishedRatingProvider"]
