"""Wire shapes for a player's ratings — A64-020.0A.

Pydantic models built from `rating.public` values, never from ORM rows.

## Every key, always — including the ones a player has not played

`RatingSnapshot.unrated()` is what `rating` returns for a key with no row,
so a summary lists all of them and marks the untouched ones `provisional`
with zero games. The alternative — omitting them — pushes "has this player
played blitz?" onto every client, and a client that got it wrong would show
a missing rating as a rating of zero.

## The Glicko-2 triple is not published in full

`value`, `deviation` and `games_played` are; `volatility` is not. It is an
input to the next calculation rather than a fact about the player, it means
nothing to a reader, and publishing it would invite a client to render a
number whose scale is an implementation detail of the algorithm.
"""

from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.rating.public import RatingKey, RatingSnapshot, SpeedClass


class RatingResponse(BaseModel):
    """One player's rating in one `(variant, speed_class)` key."""

    variant: str
    speed_class: str
    rating: float = Field(description="The Glicko-2 rating value.")
    deviation: float = Field(description="How sure the platform is. Higher means less certain.")
    games_played: int
    is_provisional: bool = Field(
        description="Fewer rated games than the threshold. Shown, never hidden."
    )

    @classmethod
    def of(cls, key: RatingKey, snapshot: RatingSnapshot) -> "RatingResponse":
        return cls(
            variant=key.variant.value,
            speed_class=key.speed_class.value,
            rating=snapshot.value,
            deviation=snapshot.deviation,
            games_played=snapshot.games_played,
            is_provisional=snapshot.is_provisional,
        )


class PlayerRatingsResponse(BaseModel):
    """Every key a player has a standing in, played or not.

    Ordered by speed class in the enum's own order — bullet to classical —
    so a client renders a stable row of tabs rather than one that reshuffles
    when a rating appears.
    """

    player_id: UUID
    ratings: list[RatingResponse]

    @classmethod
    def of(
        cls,
        player_id: UUID,
        snapshots: dict[tuple[UUID, RatingKey], RatingSnapshot],
        *,
        keys: list[RatingKey],
    ) -> "PlayerRatingsResponse":
        return cls(
            player_id=player_id,
            ratings=[
                RatingResponse.of(key, snapshots[player_id, key])
                for key in keys
                if (player_id, key) in snapshots
            ],
        )


def every_key(variant: str) -> list[RatingKey]:
    """One key per speed class, in the enum's declared order.

    Built here rather than by the route so the order is one fact: a client
    rendering tabs from this list and a test asserting on it read the same
    sequence.
    """
    from app.modules.game.public import ProductVariant

    product = ProductVariant(variant)
    return [RatingKey(variant=product, speed_class=speed) for speed in SpeedClass]


__all__ = ["PlayerRatingsResponse", "RatingResponse", "every_key"]
