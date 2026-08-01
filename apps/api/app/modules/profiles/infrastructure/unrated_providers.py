"""The two providers standing in for `rating` and `statistics`.

A64-012.1: "For now return default values if game statistics are not yet
implemented. Design the API for future compatibility."

The second sentence is the harder half, and it is why these are adapters
behind ports rather than constants in a service. The *shape* they return is
the shape the real systems will return — a complete `PlayerRatings` with a
provisional marker per category (PR-6), and a `PlayerStatistics` whose
counts sum exactly — so the day `rating` ships, no client changes and no
field appears that was not already there.

## What these do not do

They do not read anything, they cannot fail, and they are stateless. Both
ignore the `player_id` they are given, which is the honest signature: the
answer is the same for every player because no match has ever been
recorded. Accepting the argument anyway keeps them substitutable for the
real providers, which very much will use it.

They are `async` for the same reason — the port is `async` because every
real implementation is a network or database read, and a synchronous
placeholder would force the port to be synchronous and then force it back.
"""

from uuid import UUID

from app.modules.profiles.domain.ratings import PlayerRatings
from app.modules.profiles.domain.statistics import NO_MATCHES_PLAYED, PlayerStatistics


class UnratedRatingProvider:
    """Every player is unrated, because no match has been played.

    Returns `PlayerRatings.unrated()`: each category at `STARTING_RATING`,
    each marked `is_provisional=True`, each with `games_played=0`.

    The provisional marker is the load-bearing part. Without it this would
    be publishing 1500 as though it were a measurement, which is precisely
    what domain-model.md PR-6 forbids ("an unmarked provisional rating
    misleads both the opponent and the matchmaker"). With it, the response
    says what is true: a starting value, based on nothing.
    """

    async def ratings_for(self, player_id: UUID) -> PlayerRatings:
        return PlayerRatings.unrated()


class NoMatchesStatisticsProvider:
    """Every player has finished no matches.

    Returns `NO_MATCHES_PLAYED` — all four counts zero, and therefore a
    `win_rate` of `0.0` rather than a division by zero. The frozen
    singleton is safe to share: `PlayerStatistics` is immutable, so no
    caller can mutate the value another caller is holding.
    """

    async def statistics_for(self, player_id: UUID) -> PlayerStatistics:
        return NO_MATCHES_PLAYED
