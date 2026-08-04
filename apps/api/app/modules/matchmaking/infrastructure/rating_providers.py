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

## Why this adapter exists rather than handing the reader straight over

Two vocabularies meet here and neither should learn the other's. `rating`
answers by `RatingKey` — `(variant, speed class)` — and `matchmaking` asks
by `QueueType`, which is `ranked` or `casual` and says nothing about a
speed. Something has to translate, and doing it here keeps `QueueService`
free of a rating key and keeps `rating` free of a queue concept.

## The variant, and why it is a constant here

The port's signature carries no variant, because when it was written the
platform had one and `QueueType` was the only axis. It still has one
(`ProductVariant.RUSSIAN_8X8`), so the translation is exact rather than a
guess. When a second variant ships, the port's argument widens to a
`QueuePool` — which already carries a variant — and this class reads it
instead of the constant.

## Casual games are rated *reads*, not rated *writes*

A `casual` ticket still gets a real rating, because pairing a casual game by
skill is what makes it a game rather than a coin toss. What `casual` decides
is whether the *result* moves anything, and that is `rated` on the match
(SPEC-RATING §9) — a decision `game` and `rating` make, not this one.
"""

import logging
from uuid import UUID

from app.modules.game.public import ProductVariant
from app.modules.matchmaking.domain.queue_pool import QueueType
from app.modules.rating.public import DEFAULT_SPEED_CLASS, RatingKey, RatingReader, RatingSnapshot

logger = logging.getLogger(__name__)

#: The variant every pool offers today — see this module's docstring on why
#: this is a constant rather than a lookup, and what replaces it.
_VARIANT = ProductVariant.RUSSIAN_8X8


class PublishedRatingProvider:
    """Reads a player's rating through `rating`'s published surface.

    Holds a `RatingReader` and nothing else, so it cannot move a rating,
    read an adjustment, or reach `rating`'s persistence — R-4's one-way
    chain as a constructor argument rather than a rule to remember.
    """

    def __init__(self, ratings: RatingReader) -> None:
        self._ratings = ratings

    async def rating_for(self, player_id: UUID, *, queue_type: QueueType) -> RatingSnapshot:
        """The player's triple in the key this pool rates in.

        `queue_type` is accepted and deliberately unused: ranked and casual
        pools rate against the **same** key, because skill does not change
        with whether the result counts — see this module's docstring. The
        argument stays because it is the port's, and a narrower signature
        here would stop this class satisfying it.
        """
        return await self._ratings.rating_for(
            player_id, key=RatingKey(variant=_VARIANT, speed_class=DEFAULT_SPEED_CLASS)
        )


__all__ = ["PublishedRatingProvider"]
