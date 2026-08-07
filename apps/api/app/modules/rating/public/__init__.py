"""`rating`'s published surface — the only way into this module.

    RatingKey, SpeedClass         what a rating is *of*
    LeaderboardReader             standings for one key, paginated by cursor
    RatingSnapshot                what a player rates, as a Glicko-2 triple
    RatingReader                  the one question this module answers publicly
    RatingChange                  what one match did to one player's rating
    MatchRatingAdjustmentReader   that, for a page of matches, in one query

Everything else is private and is held so by the `import-linter` contract
`rating-internals-are-private`: no module may reach `rating.domain`,
`rating.application` or `rating.infrastructure`.

That matters more here than for most contexts. R-4 makes `game → rating →
leaderboard` a one-way chain, and nothing published here can write — so
"ratings must never depend on leaderboard state, or a leaderboard rebuild
could alter historical ratings" is a property of the types rather than a
rule somebody has to remember.

## What is deliberately absent: `RatingCategory`

`profiles` ships `ratings.{classic, rapid, blitz}` on its public response
and keeps doing so (SPEC-RATING §14) — but that is **`profiles`' wire
spelling**, and translating it is `profiles`' job. Publishing the mapping
here would make `rating` depend on another context's presentation contract,
and would put a deprecated name in the vocabulary every future consumer
reads.

So this module speaks `SpeedClass` only, and the alias lives at the one
boundary that needs it.
"""

from app.modules.rating.domain.keys import DEFAULT_SPEED_CLASS, RatingKey, SpeedClass
from app.modules.rating.public.adjustments import MatchRatingAdjustmentReader, RatingChange
from app.modules.rating.public.leaderboard import (
    LeaderboardCursor,
    LeaderboardEntry,
    LeaderboardPage,
    LeaderboardReader,
)
from app.modules.rating.public.ratings import RatingReader, RatingSnapshot

__all__ = [
    "DEFAULT_SPEED_CLASS",
    "MatchRatingAdjustmentReader",
    "LeaderboardCursor",
    "LeaderboardEntry",
    "LeaderboardPage",
    "LeaderboardReader",
    "RatingChange",
    "RatingKey",
    "RatingReader",
    "RatingSnapshot",
    "SpeedClass",
]
