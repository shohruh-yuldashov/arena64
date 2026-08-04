"""`RatingProvider` over `rating.public` — SPEC-RATING §14, A64-017.2.

The adapter that replaces `UnratedRatingProvider`, and the **one** place on
this platform where `RatingCategory` is translated.

## Why the old wire spelling survives

`GET /profiles/{handle}` and `GET /profile` have shipped
`ratings.{classic, rapid, blitz}` since A64-012.1. `profiles.domain.ratings`
recorded the conflict at the time and deliberately left it open:

> reconciling the two names is a decision for whoever builds `rating` — at
> which point one of the two has to move and the choice should be made once,
> deliberately, with a migration if it lands on the database side.

It has moved. `SpeedClass.CLASSICAL` is the platform's vocabulary;
`classic` is an alias at this boundary. Breaking the response instead would
have broken every existing client for a spelling.

## The rule this file exists to keep

**Nothing below presentation knows `RatingCategory` exists.** `rating`
speaks `SpeedClass` only and does not import this module — publishing the
mapping there would have made the rating system depend on another context's
wire contract, and would have put a deprecated name in the vocabulary every
future consumer reads.

New business logic must not reach for `RatingCategory`. It is a wire
spelling with a removal date (SPEC-RATING §14's `ratings_by_key`), not a
concept.

## Why an exhaustive map rather than a lookup with a default

A `RatingCategory` member added without a speed class fails at **import**
rather than rendering a profile with a missing rating. `BULLET` and
`CORRESPONDENCE` have no category and are deliberately absent from the
response — A64-012.1's scope, not a claim that they will never be rated.

## Why every category is read even though one is reachable

SPEC-RATING §8 makes `CLASSICAL` the only key a match can rate in, so
`RAPID` and `BLITZ` are always unrated today. They are read anyway, through
the same batch, because the day a second class activates this file needs no
change — and reading three keys for a page of fifty players is three
batched queries, not fifty.
"""

from collections.abc import Mapping, Sequence
from typing import Final
from uuid import UUID

from app.modules.game.public import ProductVariant
from app.modules.profiles.domain.ratings import PlayerRatings, RatingCategory, RatingSnapshot
from app.modules.rating.public import RatingKey, RatingReader, SpeedClass
from app.modules.rating.public import RatingSnapshot as PublishedRating

#: The one translation between the shipped wire spelling and the platform's.
#:
#: Deprecated on the `RatingCategory` side: it goes when the profile
#: response drops `ratings` in favour of `ratings_by_key`.
_SPEED_CLASSES: Final[dict[RatingCategory, SpeedClass]] = {
    RatingCategory.CLASSIC: SpeedClass.CLASSICAL,
    RatingCategory.RAPID: SpeedClass.RAPID,
    RatingCategory.BLITZ: SpeedClass.BLITZ,
}

if set(_SPEED_CLASSES) != set(RatingCategory):  # pragma: no cover — import-time guard
    raise RuntimeError("every RatingCategory must map to a SpeedClass")

#: The variant a public profile reports ratings for.
#:
#: One today, and the profile response has no variant dimension — it says
#: `ratings.classic`, not `ratings.russian_8x8.classic`. When a second
#: variant ships this response has to gain one, which is the same change
#: `ratings_by_key` already makes; until then this is the only honest
#: reading of a field that does not name a variant.
_PROFILE_VARIANT: Final = ProductVariant.RUSSIAN_8X8


class PublishedRatingProvider:
    """`profiles.application.ports.RatingProvider` over `rating.public`.

    Holds the published reader and nothing else: it cannot move a rating,
    cannot read the adjustment history, and cannot reach `rating`'s
    persistence — which is R-4's one-way chain expressed as a constructor
    argument.
    """

    def __init__(self, ratings: RatingReader) -> None:
        self._ratings = ratings

    async def ratings_for(self, player_id: UUID) -> PlayerRatings:
        """One player's ratings, in the shape the profile response expects."""
        ratings = await self.ratings_for_many([player_id])
        return ratings[player_id]

    async def ratings_for_many(self, player_ids: Sequence[UUID]) -> Mapping[UUID, PlayerRatings]:
        """The same, for a page of players.

        **One query for the whole page**, across every category: the caller
        is a search result or a friend list, and both a loop over players
        and a loop over categories are the same N+1 seen from different
        sides. `ratings_across` collapses both.
        """
        keys = {category: _key_for(category) for category in RatingCategory}
        across = await self._ratings.ratings_across(player_ids, keys=list(keys.values()))
        return {
            player_id: _as_player_ratings(
                {category: across[(player_id, key)] for category, key in keys.items()}
            )
            for player_id in player_ids
        }


def _key_for(category: RatingCategory) -> RatingKey:
    return RatingKey(variant=_PROFILE_VARIANT, speed_class=_SPEED_CLASSES[category])


def _as_player_ratings(
    published: Mapping[RatingCategory, PublishedRating],
) -> PlayerRatings:
    """`rating`'s triples as the profile's three named fields.

    **The deviation and the volatility are dropped**, deliberately.
    `profiles.domain.ratings` argued when it shipped that they are
    matchmaking internals and are not published to a stranger reading a
    profile; that argument stands. What crosses is the value, the count and
    PR-6's provisional mark.

    The value is **rounded** here rather than stored rounded: Glicko-2's
    arithmetic is floating point, and a player is shown 1487 rather than
    1487.3142.
    """
    return PlayerRatings(
        classic=_as_snapshot(published[RatingCategory.CLASSIC]),
        rapid=_as_snapshot(published[RatingCategory.RAPID]),
        blitz=_as_snapshot(published[RatingCategory.BLITZ]),
    )


def _as_snapshot(published: PublishedRating) -> RatingSnapshot:
    return RatingSnapshot(
        rating=round(published.value),
        games_played=published.games_played,
        is_provisional=published.is_provisional,
    )


__all__ = ["PublishedRatingProvider"]
