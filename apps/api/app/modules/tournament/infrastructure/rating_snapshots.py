"""`RatingSnapshots` over `rating.public` — §3.

The adapter that turns "this tournament's key" into a batch read. It exists
so `tournament`'s use case holds a one-method port rather than `rating`'s
reader: it can ask for a page of ratings and cannot reach an adjustment, a
leaderboard, or anything that writes.

**One call, not one per player.** A field of 128 read individually is the
N+1 §3 forbids — and the reason is correctness rather than speed: seeding
reads ratings *at a moment*, and a per-player loop spreads that moment
across the field, so a rating that moves mid-seed could place a player above
somebody they should be below.
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.game.public import ProductVariant
from app.modules.rating.public import RatingKey, RatingReader, RatingSnapshot, SpeedClass


class PublishedRatingSnapshots:
    """`RatingSnapshots` backed by `rating.public.RatingReader`."""

    def __init__(self, ratings: RatingReader) -> None:
        self._ratings = ratings

    async def ratings_for(
        self,
        player_ids: Sequence[UUID],
        *,
        variant: ProductVariant,
        speed_class: SpeedClass,
    ) -> Mapping[UUID, RatingSnapshot]:
        """Every named player's rating in one key, in one query.

        The key is built here from the tournament's own variant and speed
        class — §3's "do not infer or read another rating key". A caller
        cannot pass a key, so there is no way to seed against ratings from
        a pool the tournament is not played in.
        """
        return await self._ratings.ratings_for(
            player_ids, key=RatingKey(variant=variant, speed_class=speed_class)
        )


__all__ = ["PublishedRatingSnapshots"]
