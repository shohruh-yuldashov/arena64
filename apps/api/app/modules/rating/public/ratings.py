"""`rating`'s published surface — SPEC-RATING §7.6, and the port
`matchmaking` has been holding since A64-014.1.

Two things cross this boundary and nothing else:

    RatingSnapshot   a player's triple in one key, plus what a reader needs
                     to render it honestly
    RatingReader     "what does this player rate in this key"

## Why the snapshot is a triple and not a number

`matchmaking.application.ports.RatingSnapshotProvider` returns an `int`
today, because when it was written the rating *system* was an open question
(domain-model.md Q-3) and an integer was the one shape that did not
presuppose the answer. ADR-001 answered it, and a Glicko-2 rating is a
triple.

The seat snapshot (§7.6) is why the whole triple must cross rather than just
the value: PR-3 requires the rating calculation to run on the values captured
at match creation, and a calculation needs the deviation and the volatility.
A snapshot that carried only the rating would make PR-3 unimplementable, and
the failure would appear as two concurrent matches computing against each
other's partial results — which is not visible in any test that plays one
game at a time.

## Why `is_provisional` and `games_played` cross too

PR-6: *"provisional ratings are visibly marked everywhere they appear."*
"Everywhere" includes a seat snapshot that will be read back months later to
explain a rating change, and a public profile. Neither can derive the mark
without the count, and a consumer that computed `games_played < 25` for
itself would be a second copy of a product threshold.

## What does not cross

No `PlayerRating` aggregate, no `RatingAdjustment`, no repository, and
nothing that can write. A consumer of this module can ask what somebody
rates and cannot move it — which is R-4's one-way `game → rating →
leaderboard` chain expressed as a type rather than as a rule to remember.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.rating.domain.glicko2 import (
    INITIAL_DEVIATION,
    INITIAL_RATING,
    INITIAL_VOLATILITY,
)
from app.modules.rating.domain.keys import RatingKey


@dataclass(frozen=True, slots=True)
class RatingSnapshot:
    """One player's rating in one key, as of the read.

    Frozen: a reading, not an accumulator. Whatever moves a rating owns its
    own write model, and a consumer holding a mutable snapshot would be one
    that could appear to change a rating by editing its copy.
    """

    value: float
    deviation: float
    volatility: float

    games_played: int
    is_provisional: bool
    """PR-6's mark, computed by `rating` rather than by each consumer — see
    this module's docstring on why the threshold does not travel."""

    @classmethod
    def unrated(cls) -> "RatingSnapshot":
        """What a player who has never played this key rates at.

        The same values `PlayerRating.unrated` produces, because they are
        the same state seen from two sides: SPEC-RATING §7.5 writes no row
        until the first rated match, so a reader that finds nothing answers
        with this rather than with `None`.

        `None` would push "has this player ever played" onto every consumer,
        and a matchmaker that had to answer it before pairing would be one
        that could get it wrong.
        """
        return cls(
            value=INITIAL_RATING,
            deviation=INITIAL_DEVIATION,
            volatility=INITIAL_VOLATILITY,
            games_played=0,
            is_provisional=True,
        )


class RatingReader(Protocol):
    """What a player rates — the only question this module answers publicly.

    Read-only by construction. There is deliberately no published method
    that applies a rating: an adjustment happens in response to
    `game.match_completed` and nowhere else, so a caller that could request
    one would be a second path to the platform's most protected invariant.
    """

    async def rating_for(self, player_id: UUID, *, key: RatingKey) -> RatingSnapshot:
        """This player's rating in this key. Never `None` — see
        `RatingSnapshot.unrated`."""
        ...

    async def ratings_for(
        self, player_ids: Sequence[UUID], *, key: RatingKey
    ) -> Mapping[UUID, RatingSnapshot]:
        """The same, batched, for every named player.

        Batched because the two callers are a pairing scan and a profile
        composition, and both hold a list. A caller looping `rating_for`
        would be the N+1 this exists to prevent on the hottest read the
        matchmaker has.

        **Complete**: every id maps to a snapshot, unrated players included,
        so a caller cannot silently skip somebody by reading a key that is
        absent.
        """
        ...

    async def ratings_across(
        self, player_ids: Sequence[UUID], *, keys: Sequence[RatingKey]
    ) -> Mapping[tuple[UUID, RatingKey], RatingSnapshot]:
        """Every named player's rating in every named key, in **one** query.

        For a caller that renders several keys at once — a public profile
        shows three speed classes side by side. Looping `ratings_for` over
        keys is one query per key per page, which is the same N+1 the batch
        above exists to prevent, moved one dimension over.

        Complete on both axes: every `(player, key)` pair is present.
        """
        ...


__all__ = ["RatingReader", "RatingSnapshot"]
