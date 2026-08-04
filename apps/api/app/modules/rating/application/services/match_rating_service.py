"""`MatchRatingService` — one completed match, two ratings, one transaction.

SPEC-RATING §7.3, §9, §10, §11. The only writer in this module.

    match_completed  ->  is it rateable?  ->  load both  ->  compute both
                     ->  insert both adjustments  ->  save both  ->  publish

## Both players, or neither

§4 of the task and PR-1 together: a match that moved one player's rating and
not the other's is a ladder that no longer sums, and A-4 makes it permanent.
So both adjustments are written inside one unit of work and nothing commits
until both have succeeded.

That is also why the frozen check happens **before** either write. A
`RatingFrozen` raised halfway would abort a transaction that had already
written the other player — the transaction would roll back, so the data
would be consistent, but the *decision* would depend on which seat was
processed first. Checking both up front makes the refusal a property of the
match rather than of an ordering.

## The inputs are the seat snapshots, and nothing else

PR-3. Every number the arithmetic sees comes off `MatchCompleted`:

    who played           the event's seats, not a roster read
    what they rated      the snapshot captured at match creation
    which key            the event's variant and speed class, not inferred
    which rules          the event's engine version, not the current build

Nothing here reads a current rating, and the repository's `load` is used
only for the aggregate's *counters* — games played, peak, last rated at,
frozen — never for the triple the calculation runs on. That distinction is
the whole of PR-3, and getting it wrong is invisible until two matches
complete at once.

## Exactly-once is the database's, not this service's

A redelivered event recomputes the same numbers — the inputs are immutable —
and the unique constraint refuses the second insert.
`AdjustmentAlreadyApplied` is then a **success**: the work was done by
whoever won the race. There is deliberately no in-memory seen-set, which
would be a second answer that a restart forgets and a second process never
had.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.game.public import TerminationReason
from app.modules.rating.application.ports import (
    AdjustmentAlreadyApplied,
    PlayerRatingRepository,
)
from app.modules.rating.domain.events import RatingUpdated
from app.modules.rating.domain.glicko2 import Glicko2Rating, MatchOutcomeScore
from app.modules.rating.domain.keys import RatingKey
from app.modules.rating.domain.player_rating import (
    PlayerRating,
    RatingAdjustment,
)
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)

#: The terminations a rating is applied for — SPEC-RATING §9.
#:
#: An **allowlist**, so a termination added to `game` without a decision
#: here is silently *not* rated rather than silently rated. The first
#: direction is a missing adjustment somebody notices; the second is a
#: permanent record nobody asked for.
#:
#: `TIME_FORFEIT` is on it deliberately. A player who disconnected and lost
#: on time is rated: disconnection is not a termination — the clock is, and
#: it ran out. Treating it otherwise would make disconnecting a way to avoid
#: a loss, which is the cheapest rating-manipulation attack there is.
RATED_TERMINATIONS: Final = frozenset(
    {
        # A decisive result the engine derived from the position.
        TerminationReason.NO_LEGAL_MOVES,
        TerminationReason.ALL_PIECES_CAPTURED,
        # A player conceded.
        TerminationReason.RESIGNATION,
        # Draws, in all three forms the rules produce.
        TerminationReason.AGREED_DRAW,
        TerminationReason.REPETITION,
        TerminationReason.MOVE_LIMIT,
        # A clock ran out. **Rated**, including when the player who lost had
        # disconnected — see this constant's docstring.
        TerminationReason.FLAG,
        TerminationReason.FLAG_INSUFFICIENT_MATERIAL,
    }
)

#: What is deliberately **absent**, so a reader does not have to diff the
#: two lists:
#:
#:     ABORT           §9 — a match that never became a game
#:     ABANDONMENT     an undecided policy (domain-model.md Q-7); a match
#:                     ends this way only once a reaper exists, and whether
#:                     it rates is a product decision nobody has made
#:     ADJUDICATION    an administrative outcome — SPEC-RATING §9 excludes it
#:     WIN, DRAW, NONE placeholders `MatchOutcome` owns; not terminations


@dataclass(frozen=True, slots=True)
class CompletedSeat:
    """One seat of a completed match, as `rating` consumes it.

    Decoded from `game.match_completed`'s payload and never read back from
    `game`. See the module docstring: the whole point is that the numbers
    here are the ones captured at match creation.
    """

    player_id: UUID
    value: float
    deviation: float
    volatility: float

    def as_rating(self) -> Glicko2Rating:
        """The snapshot as the arithmetic's input.

        `games_played` and `is_provisional` travel on the event too but are
        not needed here: they describe the player, and Glicko-2 takes only
        the triple.
        """
        return Glicko2Rating(value=self.value, deviation=self.deviation, volatility=self.volatility)


@dataclass(frozen=True, slots=True)
class CompletedMatch:
    """Everything `rating` needs about one finished match.

    Assembled from the event's payload alone (§2 of A64-017.3: do not infer
    the speed class, the participants, or the engine version). If a field is
    missing the match is simply not rateable — inventing one would put a
    made-up number on a permanent record.
    """

    match_id: UUID
    key: RatingKey
    rated: bool
    termination: TerminationReason
    winner: str | None
    light: CompletedSeat
    dark: CompletedSeat

    @property
    def is_rateable(self) -> bool:
        """Whether this completion moves ratings — SPEC-RATING §9.

        Three conditions, and all must hold: the match was rated, it ended
        in a way the allowlist covers, and the two seats are different
        people. The last is a guard against a data defect rather than a
        policy — `matchmaking` never pairs a player with themselves.
        """
        return (
            self.rated
            and self.termination in RATED_TERMINATIONS
            and self.light.player_id != self.dark.player_id
        )

    @property
    def light_score(self) -> MatchOutcomeScore:
        """Light's result, from which dark's is derived by inversion.

        One score computed and one inverted, never two computed: two
        independent derivations can both say "win", and a match that awarded
        both players a victory is a ladder nobody can trust.
        """
        if self.winner is None:
            return MatchOutcomeScore.draw()
        return MatchOutcomeScore.win() if self.winner == "light" else MatchOutcomeScore.loss()


class MatchRatingOutcome:
    """Why a completion did or did not move a rating. For metrics and logs."""

    APPLIED: Final = "applied"
    ALREADY_APPLIED: Final = "already_applied"
    NOT_RATEABLE: Final = "not_rateable"
    FROZEN: Final = "frozen"


class MatchRatingService:
    """Applies one completed match to both players' ratings."""

    def __init__(
        self,
        *,
        ratings: PlayerRatingRepository,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._ratings = ratings
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def apply(self, completion: CompletedMatch) -> str:
        """Rates one match. Returns why, from `MatchRatingOutcome`.

        Returns rather than raises for the two *expected* outcomes — a
        casual match and a redelivery — because neither is a failure and a
        consumer that had to catch them would treat the ordinary case as
        exceptional. A genuine fault still propagates.
        """
        if not completion.is_rateable:
            return MatchRatingOutcome.NOT_RATEABLE

        async with self._unit_of_work:
            light = await self._ratings.load(completion.light.player_id, key=completion.key)
            dark = await self._ratings.load(completion.dark.player_id, key=completion.key)

            # **Before either write** — see this module's docstring on why
            # the refusal must be a property of the match rather than of
            # which seat happened to be processed first.
            if light.is_frozen or dark.is_frozen:
                logger.warning(
                    "rating_refused_frozen", extra={"match_id": str(completion.match_id)}
                )
                await self._unit_of_work.rollback()
                return MatchRatingOutcome.FROZEN

            at = self._clock.now()
            try:
                updates = self._computed(completion, light=light, dark=dark, at=at)
                for rating, adjustment in updates:
                    await self._ratings.save(rating, adjustment)
            except AdjustmentAlreadyApplied:
                # A relay redelivered. The winner of the race wrote exactly
                # these numbers, because the inputs are immutable.
                await self._unit_of_work.rollback()
                return MatchRatingOutcome.ALREADY_APPLIED

            for rating, adjustment in updates:
                await self._events.publish(_updated(rating, adjustment))

            await self._unit_of_work.commit()

        logger.info(
            "rating_applied",
            extra={"match_id": str(completion.match_id), "key": str(completion.key)},
        )
        return MatchRatingOutcome.APPLIED

    def _computed(
        self,
        completion: CompletedMatch,
        *,
        light: PlayerRating,
        dark: PlayerRating,
        at: datetime,
    ) -> list[tuple[PlayerRating, RatingAdjustment]]:
        """Both sides' new ratings, from the seat snapshots alone.

        **Each side is computed against the other's snapshot**, not against
        the other's freshly-updated rating: the two updates are simultaneous
        by definition, and sequencing them would make the second player's
        result depend on the first's — which is the concurrency bug PR-3
        exists to prevent, reintroduced inside one transaction.
        """
        light_seat = completion.light.as_rating()
        dark_seat = completion.dark.as_rating()
        light_score = completion.light_score

        # `based_on` is PR-3 made structural: the counters come from the
        # stored aggregate and the triple from the seat snapshot. Calling
        # `applied` on the loaded aggregate directly would compute from
        # whatever the player rates *now*, which is the one thing PR-3
        # forbids — and which no single-match test can detect.
        return [
            light.based_on(light_seat).applied(
                opponent=dark_seat,
                score=light_score,
                match_id=completion.match_id,
                at=at,
            ),
            dark.based_on(dark_seat).applied(
                opponent=light_seat,
                score=light_score.inverted(),
                match_id=completion.match_id,
                at=at,
            ),
        ]


def _updated(rating: PlayerRating, adjustment: RatingAdjustment) -> RatingUpdated:
    return RatingUpdated(
        occurred_at=adjustment.applied_at,
        player_id=rating.player_id,
        match_id=adjustment.match_id,
        variant=rating.key.variant.value,
        speed_class=rating.key.speed_class.value,
        rating_before=adjustment.before.value,
        rating_after=adjustment.after.value,
        deviation_after=adjustment.after.deviation,
        volatility_after=adjustment.after.volatility,
        games_played=rating.games_played,
        is_provisional=rating.is_provisional,
        algorithm_version=adjustment.algorithm_version,
    )


__all__ = [
    "RATED_TERMINATIONS",
    "CompletedMatch",
    "CompletedSeat",
    "MatchRatingOutcome",
    "MatchRatingService",
]
