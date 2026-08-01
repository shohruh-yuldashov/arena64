"""The implementation of `application.ports.RatingProvider` — AD-06: the
port is declared in `application/`, satisfied here.

One class, and it is the last placeholder in this module. A64-012.8 moved
it out of `statistics_providers.py`, where A64-012.6 had left it: that file
was renamed from `unrated_providers.py` when the *statistics* placeholder
stopped being one, and a rating provider sitting in a file named for
statistics is a file whose name no longer says what is in it. Two ports,
two files, and a reader looking for the rating adapter finds it where its
name says it is.

Nothing about the class changed in the move.
"""

from uuid import UUID

from app.modules.profiles.domain.ratings import PlayerRatings


class UnratedRatingProvider:
    """Every player is unrated, because no match has been played.

    Still a placeholder, and the only one left on this module's four
    sources — `rating` has no module, no spec implementation and nothing
    that produces a result to rate.

    Returns `PlayerRatings.unrated()`: each category at `STARTING_RATING`,
    each marked `is_provisional=True`, each with `games_played=0`.

    The provisional marker is the load-bearing part. Without it this would
    be publishing 1500 as though it were a measurement, which is precisely
    what domain-model.md PR-6 forbids ("an unmarked provisional rating
    misleads both the opponent and the matchmaker"). With it, the response
    says what is true: a starting value, based on nothing.

    **Not a stub.** A stub is a hole that reads as supported; this is a
    total, correct implementation of a well-defined state. A player who has
    played nothing genuinely has no rating, and this keeps producing the
    right answer for a brand-new account after `rating` ships.

    Stateless and infallible, and it ignores the `player_id` it is given —
    the honest signature, since the answer is the same for every player.
    Accepting the argument anyway keeps it substitutable for the real
    provider, which very much will use it. `async` because the port is, and
    the port is because every real implementation is a network read.
    """

    async def ratings_for(self, player_id: UUID) -> PlayerRatings:
        return PlayerRatings.unrated()
