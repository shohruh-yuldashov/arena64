"""The ports `profiles` programs against — AD-06: declared in
`application/`, satisfied by `infrastructure/` or by another module's
published surface.

Three sources, three ports, and only one of them has a real implementation
today.

    PublicProfileReader   `users.public` — identity. Real.
    RatingProvider        a rating system. Placeholder.
    StatisticsProvider    a statistics system. Placeholder.

## Why the two unbuilt sources get ports rather than inline defaults

`ProfileService` could return `PlayerRatings.unrated()` directly and save
two files. The ports exist because of what happens next: when `rating`
ships, the difference between those two designs is a *new adapter* against
an interface the service already uses, versus editing the service that
serves the platform's most-read public endpoint.

That is AD-08's shape for cross-context reads, and it is also the only way
the placeholder can be honest. A default buried in a service reads as
data; a `UnratedRatingProvider` in `infrastructure/` cannot be mistaken
for one — its name is the disclosure.

This is not the "empty `login()` waiting to be filled in" that this
codebase refuses elsewhere. A stub is a *hole* that reads as supported.
These are total, correct implementations of a well-defined state — a
player who has played nothing genuinely has no rating and no record — and
A64-012.1 asks for exactly that ("For now return default values if game
statistics are not yet implemented"). The distinction is that these
produce the right answer for every player today, and will keep producing
the right answer for a brand-new account after `rating` ships.

`PublicProfileReader` is deliberately **not** redeclared here. It is
`users.public`'s type, imported and used as-is: a local re-declaration
would be a second definition of a contract that already has an owner, and
BR-2 requires a `public/` port be consumed in terms of the DTOs it
publishes.
"""

from typing import Protocol
from uuid import UUID

from app.modules.profiles.domain.ratings import PlayerRatings
from app.modules.profiles.domain.statistics import PlayerStatistics


class RatingProvider(Protocol):
    """Reads a player's current rating in every reported category.

    A `Protocol`, not an ABC, so the placeholder and a future `rating`
    adapter satisfy it structurally without either inheriting from
    anything this module owns.

    Takes a `UUID` — DM-06's `player_id`, the only reference that crosses a
    context boundary. Deliberately not a `PublicUserProfile`: a rating
    system has no business receiving a display name, and a port that
    accepted one would make `profiles` the reason it could read it.
    """

    async def ratings_for(self, player_id: UUID) -> PlayerRatings:
        """Every category, always.

        Returns a complete `PlayerRatings` rather than a partial map, so a
        player with no games in a category yields that category's starting
        snapshot rather than a missing key. A profile whose `ratings`
        object varies in shape by player is one every client has to write
        defensive code against.

        Never raises for an unknown player: a player with no ratings is
        the ordinary case, not a failure, and it is the same answer as a
        player who does not exist — which the caller has already
        established does not apply, because identity is resolved first.
        """
        ...


class StatisticsProvider(Protocol):
    """Reads a player's aggregate match record."""

    async def statistics_for(self, player_id: UUID) -> PlayerStatistics:
        """The counts, always. Zeroes for a player who has finished no
        matches — see `RatingProvider.ratings_for` on why absence is a
        value here rather than an exception."""
        ...
