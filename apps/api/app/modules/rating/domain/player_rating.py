"""`PlayerRating` and `RatingAdjustment` — SPEC-RATING §7.5, §7.7.

The aggregate a rating update mutates, and the permanent record of why it
moved. Both are frozen: an update produces a *new* rating and an adjustment
describing the transition, which is what makes PR-4's "record the inputs
that produced it" structural rather than something a service has to remember
to do.

## The one operation

    applied(opponent, score, at)  ->  (new PlayerRating, RatingAdjustment)

Returning both is the design. A caller that got only the new rating would
have to reconstruct the adjustment from the before-and-after it happens to
be holding, and the first caller to forget would silently produce a rating
history with a hole in it — for the one dataset A-4 says must reconcile
exactly.

Inflation happens **inside** this method, from `last_rated_at` to the
instant of the update, because SPEC-RATING §7.4's whole point is that there
is no other moment at which it could happen. A caller that had to remember
to inflate first is a caller that will forget.

## What this module does not do

No persistence, no clock, no events, no identifiers minted. `at` is passed
in (AD-07) and the match is named by the caller. That keeps the aggregate a
pure function of its inputs, which is what lets the provisional boundary and
the frozen refusal be tested without a database.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from app.core.exceptions import DomainError
from app.modules.rating.domain.glicko2 import (
    GameResult,
    Glicko2Rating,
    MatchOutcomeScore,
    expected_score,
    inflated,
    rated,
)
from app.modules.rating.domain.keys import RatingKey

#: How many rated matches in a key end provisional status — SPEC-RATING §7.5.
#:
#: A **product** decision, not a Glicko-2 one. The algorithm expresses
#: confidence as deviation, and a player's RD may still be large at 25 games
#: or small before them; both figures are reported because they answer
#: different questions — "has this player been measured enough to publish"
#: and "how sure are we of the number".
PROVISIONAL_GAMES_THRESHOLD: Final = 25

#: Stamped on every adjustment — SPEC-RATING §7.7.
#:
#: Rating systems get retuned. Without a version on each row a retune makes
#: every historical adjustment inexplicable: the stored numbers no longer
#: follow from any algorithm the platform can run, and the rating history
#: becomes undefendable in a dispute.
#:
#: Bumped when the *arithmetic or its parameters* change — a τ change is a
#: new version; a refactor that produces identical numbers is not.
ALGORITHM_VERSION: Final = "glicko2-tau0.5-v1"


class RatingFrozen(DomainError):
    """The rating is under a fair-play hold and accepts no adjustment.

    PR-5. In v0.5.0 nothing sets the flag — `fairplay` does not exist — so
    this is unreachable in production and is the extension point rather
    than dead code: the day the flag is set, the refusal already works.

    **The refused adjustment is lost, not queued.** PR-5's full rule queues
    the matches and applies or discards them when the case resolves; that
    queue is deliberately not built (SPEC-RATING §13), and the consequence
    is recorded there rather than discovered by whoever builds `fairplay`.
    """


@dataclass(frozen=True, slots=True)
class RatingAdjustment:
    """One match's permanent, immutable effect on one player's rating.

    Everything needed to answer *"why did I lose 14 points"* from stored
    data alone, without re-deriving it from an algorithm that may since have
    changed. That is PR-4, and it is why the opponent's whole triple is here
    rather than just their rating: the deviation is what decided how much
    weight the result carried.
    """

    player_id: UUID
    match_id: UUID
    key: RatingKey

    before: Glicko2Rating
    """The rating this update started from — **after** any inactivity
    inflation, so `after` follows from `before` by the algorithm alone."""

    after: Glicko2Rating
    opponent: Glicko2Rating
    """The opponent's triple as captured on their seat at match creation
    (PR-3), never their rating now."""

    expected_score: float
    actual_score: float

    algorithm_version: str
    applied_at: datetime
    season_id: UUID | None = None
    """Always `None` in v0.5.0 — SPEC-RATING §12.

    The column exists because an adjustment is permanent: a season
    introduced later cannot be written onto adjustments that have already
    happened, so the field has to exist before the first one is recorded.
    """

    @property
    def points_gained(self) -> float:
        """How far the rating moved. Negative for a loss.

        Derived rather than stored, because a stored copy is a second
        number that can disagree with the two it is derived from — and this
        is the one a player quotes back at support.
        """
        return self.after.value - self.before.value


@dataclass(frozen=True, slots=True)
class PlayerRating:
    """A player's measured skill in one `RatingKey` — the aggregate root.

    Frozen, so `applied` returns a new instance. See this module's docstring
    on why that is what makes the adjustment reliable rather than optional.
    """

    player_id: UUID
    key: RatingKey
    rating: Glicko2Rating

    games_played: int = 0
    """Matches that moved **this** rating. Not the player's total, and not
    `statistics`' count of every game they finished — a player with 200
    casual games and 3 rated ones is provisional here and experienced
    there, and both are true."""

    is_frozen: bool = False
    peak_value: float | None = None
    peak_at: datetime | None = None

    last_rated_at: datetime | None = None
    """When this rating last moved. The clock lazy inflation measures from
    (§7.4) — `None` for a player whose first rated match this is, which is
    why inflation is skipped rather than computed from the epoch."""

    season_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.games_played < 0:
            raise ValueError("games_played cannot be negative")

    @classmethod
    def unrated(cls, player_id: UUID, key: RatingKey) -> "PlayerRating":
        """What a player who has never played this key rates at.

        Constructed rather than persisted: SPEC-RATING §7.5 creates the row
        on the **first rated match**, because a rating with no games is a
        claim rather than a measurement. A reader that finds no row answers
        with this, so "absent" and "1500, provisional, zero games" are the
        same state seen from two sides.
        """
        return cls(player_id=player_id, key=key, rating=Glicko2Rating.initial())

    @property
    def is_provisional(self) -> bool:
        """PR-6's mark: fewer than `PROVISIONAL_GAMES_THRESHOLD` rated games.

        Derived, never stored. A stored flag is a second copy of what
        `games_played` already says, and the copy is what goes stale — on
        the one dataset A-4 forbids being wrong about.
        """
        return self.games_played < PROVISIONAL_GAMES_THRESHOLD

    def applied(
        self,
        *,
        opponent: Glicko2Rating,
        score: MatchOutcomeScore,
        match_id: UUID,
        at: datetime,
    ) -> tuple["PlayerRating", RatingAdjustment]:
        """This rating after one match, and the record of the change.

        The whole update, in order:

            inflate for absence  ->  Glicko-2  ->  count the game
                                 ->  record the peak

        `opponent` is the triple captured on their seat at match creation
        (PR-3) — **never** their current rating. Two matches completing
        concurrently would otherwise each compute against the other's
        partial result, and neither would be reproducible from the record.

        Raises `RatingFrozen` rather than returning an unchanged rating: a
        silent no-op would make a fair-play hold indistinguishable from a
        match that happened to move nothing, and the caller has to count the
        refusal (SPEC-RATING §17).
        """
        if self.is_frozen:
            raise RatingFrozen(f"rating for {self.key} is frozen and accepts no adjustment")

        before = inflated(self.rating, elapsed_seconds=self._idle_seconds(at))
        after = rated(before, [GameResult(opponent=opponent, score=score)])

        updated = PlayerRating(
            player_id=self.player_id,
            key=self.key,
            rating=after,
            games_played=self.games_played + 1,
            is_frozen=self.is_frozen,
            peak_value=max(after.value, self.peak_value or after.value),
            # The instant the peak was *reached*, so a rating that rose and
            # fell keeps the date it was highest rather than the date it was
            # last touched.
            peak_at=at if self._is_new_peak(after.value) else self.peak_at,
            last_rated_at=at,
            season_id=self.season_id,
        )

        adjustment = RatingAdjustment(
            player_id=self.player_id,
            match_id=match_id,
            key=self.key,
            before=before,
            after=after,
            opponent=opponent,
            # Computed from `before` — the inflated rating the update
            # actually ran on — so the recorded expectation is the one the
            # arithmetic used rather than one a reader would recompute
            # differently.
            expected_score=expected_score(before, opponent),
            actual_score=score.value,
            algorithm_version=ALGORITHM_VERSION,
            applied_at=at,
            season_id=self.season_id,
        )

        return updated, adjustment

    def frozen(self) -> "PlayerRating":
        """This rating, held against adjustment — PR-5.

        Unreachable in v0.5.0: nothing sets it, because `fairplay` does not
        exist. Present so that the day it does, freezing is one call rather
        than a change to this aggregate.
        """
        return PlayerRating(
            player_id=self.player_id,
            key=self.key,
            rating=self.rating,
            games_played=self.games_played,
            is_frozen=True,
            peak_value=self.peak_value,
            peak_at=self.peak_at,
            last_rated_at=self.last_rated_at,
            season_id=self.season_id,
        )

    def _idle_seconds(self, at: datetime) -> float:
        """How long since this rating last moved.

        Zero when it has never moved — a first match has no absence to
        measure, and inflating from the epoch would hand a new player the
        deviation ceiling for no reason. `inflated` treats zero as a no-op,
        so the two cases meet without a branch at the call site.
        """
        if self.last_rated_at is None:
            return 0.0
        return (at - self.last_rated_at).total_seconds()

    def _is_new_peak(self, value: float) -> bool:
        return self.peak_value is None or value > self.peak_value


__all__ = [
    "ALGORITHM_VERSION",
    "PROVISIONAL_GAMES_THRESHOLD",
    "PlayerRating",
    "RatingAdjustment",
    "RatingFrozen",
]
