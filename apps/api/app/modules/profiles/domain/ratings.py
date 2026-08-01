"""A player's rating in each category — the shape, not the arithmetic.

Framework-free (architecture.md §8), and deliberately free of the rating
*system* too: nothing here computes a rating, updates one, or knows what
Elo or Glicko-2 is. domain-model.md Q-3 ("Elo or Glicko-2, or another
system?") is an **open question**, and a public profile must not be the
place it gets answered by accident.

What this module fixes is only what a stranger sees, which is the part
that has to be stable before Q-3 is settled.

## Why a rating is an object on the wire and not a bare number

`{"classic": 1500}` is the obvious shape and it is a trap, because of
domain-model.md PR-6:

> Provisional ratings are visibly marked everywhere they appear — an
> unmarked provisional rating misleads both the opponent and the
> matchmaker.

A bare integer cannot carry that mark. Adding it later means changing
`ratings.classic` from a number to an object, which breaks every client
that read it — so the shape has to be right now, while there are no
clients, rather than at the moment the first real rating is computed.

The same argument extends: Q-3 resolving to Glicko-2 makes a rating a
*triple* (value, deviation, volatility). Those are matchmaking internals
and are not published here, but an object can gain them additively and a
number cannot gain anything at all.

## Why `games_played` appears per category as well as in `statistics`

They count different things and will disagree, legitimately. The profile's
`statistics.games_played` is every rated and unrated match a player has
finished; a category's is the matches that moved *that* rating. A player
with 200 casual games and 3 rated blitz games has one provisional rating
and a substantial record, and a profile showing only the aggregate would
make that look like an established blitz player.
"""

from dataclasses import dataclass
from enum import StrEnum

#: The rating a player starts with, before any match has been played.
#:
#: 1500 is the conventional origin for both Elo and Glicko-2, which is why
#: it is defensible while Q-3 is open — it is the one number that does not
#: presuppose the answer.
#:
#: It is emphatically **not zero**. A zero would render as a real rating of
#: the worst possible player rather than as "no measurement yet", and the
#: `is_provisional` flag beside it is what distinguishes those two — see
#: `RatingSnapshot`.
#:
#: A constant rather than a setting: this is a domain figure that the
#: rating system will own once it exists, and a `RATING_STARTING_VALUE`
#: environment variable would let it be changed per tier, which would make
#: two deployments' ratings incomparable.
STARTING_RATING = 1500


class RatingCategory(StrEnum):
    """The rating pools a public profile reports.

    A64-012.1 names exactly these three, and they are the wire names.

    **`CLASSIC` is spelled differently from the platform's `speed_class`
    enum**, which database.md §562 gives as
    `bullet, blitz, rapid, classical, correspondence`. That is a real
    inconsistency and it is recorded here rather than quietly resolved in
    either direction: A64-012.1 specifies `classic` as the API contract, so
    that is what ships, and reconciling the two names is a decision for
    whoever builds `rating` — at which point one of the two has to move and
    the choice should be made once, deliberately, with a migration if it
    lands on the database side.

    Note also what is *absent*: `bullet` and `correspondence` exist as
    speed classes and are not reported here. That is A64-012.1's scope
    rather than a claim that they will not be rated, and the response shape
    is a map keyed by category precisely so adding one is additive.
    """

    CLASSIC = "classic"
    RAPID = "rapid"
    BLITZ = "blitz"


@dataclass(frozen=True, slots=True)
class RatingSnapshot:
    """One category's rating as of now.

    Frozen: a reading, not an accumulator. Whatever computes ratings owns
    its own write model.
    """

    rating: int = STARTING_RATING

    games_played: int = 0
    """Matches that moved *this* rating — not the player's total. See this
    module's docstring on why the two are both reported."""

    is_provisional: bool = True
    """PR-6's mark. `True` until the rating system says otherwise.

    Defaults to `True` rather than `False`, and the direction is the
    safety property: a rating wrongly marked provisional understates
    confidence, which is recoverable and visible. A rating wrongly marked
    *established* is a claim the platform cannot support, and it misleads
    the opponent choosing whether to accept a challenge — which is exactly
    the failure PR-6 names.
    """

    def __post_init__(self) -> None:
        if self.games_played < 0:
            raise ValueError("games_played cannot be negative")
        if self.rating < 0:
            # Not a rating-system rule — no system this platform might
            # choose produces a negative rating, so one here means a
            # provider is broken rather than a player is bad.
            raise ValueError("rating cannot be negative")


@dataclass(frozen=True, slots=True)
class PlayerRatings:
    """Every category a public profile reports, for one player.

    A dataclass with three named fields rather than a `dict[RatingCategory,
    RatingSnapshot]`, so that a missing category is impossible rather than
    merely unlikely: a provider that forgot `blitz` fails to construct this
    instead of producing a profile whose `ratings` object is silently short
    a key. `as_map` is the rendering form.
    """

    classic: RatingSnapshot
    rapid: RatingSnapshot
    blitz: RatingSnapshot

    @classmethod
    def unrated(cls) -> "PlayerRatings":
        """A player who has played nothing — every category at the starting
        value, every one marked provisional.

        This is what every profile on the platform returns today, because
        no rating system exists yet. It is a named constructor rather than
        a literal at the call site so that `profiles` never hardcodes the
        starting value, and so a grep finds every place that assumed it.
        """
        return cls(
            classic=RatingSnapshot(),
            rapid=RatingSnapshot(),
            blitz=RatingSnapshot(),
        )

    def as_map(self) -> dict[RatingCategory, RatingSnapshot]:
        """Keyed by category, for rendering.

        Built from the fields rather than stored as a dict, so the two
        cannot disagree and so adding a fourth category is a change the
        type checker points at every call site of.
        """
        return {
            RatingCategory.CLASSIC: self.classic,
            RatingCategory.RAPID: self.rapid,
            RatingCategory.BLITZ: self.blitz,
        }
