"""Adjudicating absence — SPEC-TOURNAMENT §6e.

A64-019.6 makes a tournament match **system-activated**: nobody is asked to
accept a fixture they entered a tournament to play, so the match is created
already `ACTIVE` and can never expire unanswered. That removes the mechanism
that used to end a match nobody engaged with, and this is what replaces it.

    both arrived     nothing — play proceeds, and whatever happens next is
                     the game's business
    one arrived      the present player advances, by adjudication
    neither arrived  the higher seed advances, by adjudication

**No game result is fabricated.** The `game` match is left exactly as it is;
nothing here writes an outcome, a winner or a termination reason into it. So
there is no completion, no `match.completed`, and no rating adjustment —
`specs/rating.md`'s termination allowlist is untouched, exactly as for §6c's
two-draw adjudication. The advancement is a *tournament* fact recorded on the
bracket, and `AttemptOutcome.NO_SHOW` says so rather than pretending a game
was won.

## Why a real result always beats this worker

Requirement, and it is held in three independent places rather than by
ordering alone:

1. **The claim is not the decision.** A claimed attempt is re-read against
   `game`'s authoritative state inside the adjudicating transaction, and an
   attempt whose match has DECIDED or DRAWN is handed to the ordinary
   advancement path instead — the same service the outbox consumer drives,
   so a repair and a delivery cannot disagree about what a result means.
2. **Attendance stops it.** A match both players reached is never
   adjudicated for absence, whatever its deadline says. A match that is
   being *played* is by definition one both players reached, so a stale
   deadline cannot reach a started game.
3. **The write is a compare-and-set.** `complete` is guarded on
   `outcome IS NULL` and `claim_winner` on `winner_id IS NULL`, so two
   workers cannot both adjudicate and a worker that lost cannot overwrite.

## Bounded, and never raising

It runs from a schedule. A sweep that propagated would stop the schedule
that called it — `ClockAdjudicationService.adjudicate_once`'s argument — and
a failure on one attempt must not cost the others their tick, so the guard is
per attempt as well as around the pass.
"""

import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.game.public import MatchOrigin
from app.modules.game.public.reconciliation import (
    OriginMatchOutcome,
    OriginMatchReader,
    OriginMatchState,
)
from app.modules.tournament.application.ports import (
    BracketRepository,
    PairingAttemptRepository,
    TournamentRepository,
)
from app.modules.tournament.application.services.advancement_service import (
    CompletedTournamentMatch,
    TournamentAdvancementService,
    higher_seed_of,
)
from app.modules.tournament.application.services.bracket_service import (
    ConflictingWinner,
    TournamentBracketService,
)
from app.modules.tournament.domain.attempts import (
    AttemptOutcome,
    AttemptStatus,
    PairingAttempt,
    adjudicate_absence,
)
from app.modules.tournament.domain.tournament import TournamentStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NoShowOutcome:
    """What one `adjudicate_once` did.

    Returned rather than only logged — the shape every sweep on this
    platform uses. The counters are separated because an operator acts
    differently on each: `walkovers` is a player being let down by their
    opponent, `abandoned` is a fixture nobody came to at all, and a rising
    `superseded` means the sweep is racing real results rather than
    recovering from absence.
    """

    claimed: int
    walkovers: int
    """One player present. They advance."""

    abandoned: int
    """Neither player present. The higher seed advances."""

    playing: int
    """Both present — left alone. Not a no-show."""

    superseded: int
    """A real result arrived first, and was applied instead."""

    @property
    def adjudicated(self) -> int:
        return self.walkovers + self.abandoned


class TournamentNoShowService:
    """One bounded, idempotent no-show pass."""

    def __init__(
        self,
        *,
        tournaments: TournamentRepository,
        bracket: BracketRepository,
        brackets: TournamentBracketService,
        attempts: PairingAttemptRepository,
        advancement: TournamentAdvancementService,
        origin_matches: OriginMatchReader,
        unit_of_work: UnitOfWork,
        clock: Clock,
        batch_size: int,
    ) -> None:
        self._tournaments = tournaments
        self._bracket = bracket
        self._brackets = brackets
        self._attempts = attempts
        self._advancement = advancement
        self._origin_matches = origin_matches
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._batch_size = batch_size

    async def adjudicate_once(self) -> NoShowOutcome:
        """One tick. Never raises — see this module's docstring."""
        try:
            claimed = await self._claim()
        except Exception as exc:  # noqa: BLE001 — a sweep must not stop its schedule
            logger.error(
                "tournament_no_show_claim_failed",
                extra={"error": type(exc).__name__},
                exc_info=exc,
            )
            return _NOTHING

        verdicts = [await self._settle(attempt) for attempt in claimed]
        walkovers = verdicts.count(_Verdict.WALKOVER)
        abandoned = verdicts.count(_Verdict.ABANDONED)
        playing = verdicts.count(_Verdict.PLAYING)
        superseded = verdicts.count(_Verdict.SUPERSEDED)

        outcome = NoShowOutcome(
            claimed=len(claimed),
            walkovers=walkovers,
            abandoned=abandoned,
            playing=playing,
            superseded=superseded,
        )
        if outcome.claimed:
            logger.info(
                "tournament_no_show_tick",
                extra={
                    "claimed": outcome.claimed,
                    "walkovers": outcome.walkovers,
                    "abandoned": outcome.abandoned,
                    "playing": outcome.playing,
                    "superseded": outcome.superseded,
                },
            )
        return outcome

    async def _claim(self) -> list[PairingAttempt]:
        """A bounded page of lapsed attempts, claimed for this worker.

        Its own transaction, so the rows this worker took are visibly
        locked before anything else happens, and `SKIP LOCKED` is what makes
        a second sweep skip rather than wait.
        """
        async with self._unit_of_work:
            claimed = await self._attempts.claim_no_show(
                now=self._clock.now(), limit=self._batch_size
            )
            await self._unit_of_work.commit()
        return claimed

    async def _settle(self, attempt: PairingAttempt) -> "_Verdict":
        try:
            return await self._decide(attempt)
        except Exception as exc:  # noqa: BLE001 — one attempt must not stop a page
            logger.error(
                "tournament_no_show_failed",
                extra={"pairing_id": str(attempt.pairing_id), "error": type(exc).__name__},
                exc_info=exc,
            )
            return _Verdict.SKIPPED

    async def _decide(self, attempt: PairingAttempt) -> "_Verdict":
        """One attempt, re-read against authority before anything is written.

        The order is the whole safety argument: the *match* is consulted
        first, then attendance, and only then is anything decided. Reversing
        the first two would let a stale deadline adjudicate a game that had
        already been played.
        """
        located = await self._bracket.locate(attempt.pairing_id)
        if located is None:  # pragma: no cover — the attempt's foreign key
            # is to this row, so it cannot be absent while the attempt is not.
            return _Verdict.SKIPPED

        tournament = await self._tournaments.by_id(located.tournament_id)
        if tournament is None or tournament.status is not TournamentStatus.IN_PROGRESS:
            # A cancelled or completed tournament does not move — OQ-1
            # leaves cancellation to the Administration epic.
            return _Verdict.SKIPPED

        # **Authority, re-read now.** A result that arrived while this
        # attempt sat in the claim wins, and is applied through the ordinary
        # path rather than adjudicated over.
        outcomes = await self._origin_matches.outcomes_for(
            [attempt.pairing_id], origin=MatchOrigin.TOURNAMENT
        )
        current = next((o for o in outcomes if o.match_id == attempt.match_id), None)
        if current is None:  # pragma: no cover — the reconciler repairs this
            return _Verdict.SKIPPED

        if current.state in (OriginMatchState.DECIDED, OriginMatchState.DRAWN):
            await self._apply_real_result(attempt, current)
            return _Verdict.SUPERSEDED

        # A match both players reached is being played, whatever the
        # deadline says — §6e, and the guard that keeps a stale worker away
        # from a started game.
        advancement = adjudicate_absence(
            attempt, higher_seed_player_id=higher_seed_of(located.node)
        )
        if advancement is None:
            return _Verdict.PLAYING
        if advancement.winner_id is None or advancement.reason is None:  # pragma: no cover
            return _Verdict.SKIPPED

        await self._record(attempt, advancement.winner_id)
        try:
            await self._brackets.advance_winner(
                tournament.id,
                round_number=located.node.round_number,
                slot=located.node.slot,
                winner_id=advancement.winner_id,
                reason=advancement.reason,
            )
        except ConflictingWinner:
            # Somebody else advanced a different player between the re-read
            # and the write. Refused rather than resolved: nothing here can
            # know which is right, and an overwrite on a bracket means a
            # player advancing out of a node they lost.
            logger.error(
                "tournament_no_show_conflicted",
                extra={"pairing_id": str(attempt.pairing_id)},
            )
            return _Verdict.SKIPPED

        logger.warning(
            "tournament_no_show_adjudicated",
            extra={
                "tournament_id": str(tournament.id),
                "pairing_id": str(attempt.pairing_id),
                "match_id": str(attempt.match_id),
                "winner_id": str(advancement.winner_id),
                "light_present": attempt.attendance.light_present,
                "dark_present": attempt.attendance.dark_present,
            },
        )
        return _Verdict.ABANDONED if attempt.attendance.nobody_arrived else _Verdict.WALKOVER

    async def _apply_real_result(
        self, attempt: PairingAttempt, outcome: OriginMatchOutcome
    ) -> None:
        """A game that was actually played beats this sweep — requirement 5.

        Routed through the **same** advancement service the outbox consumer
        drives, so a recovery and a delivery cannot disagree about what a
        result means, and idempotent end to end so calling it on a
        maybe-handled match is safe.
        """
        await self._advancement.apply(
            CompletedTournamentMatch(
                match_id=attempt.match_id,
                pairing_id=attempt.pairing_id,
                outcome=(
                    AttemptOutcome.DECISIVE
                    if outcome.state is OriginMatchState.DECIDED
                    else AttemptOutcome.DRAW
                ),
                winner_seat=outcome.winner.value if outcome.winner is not None else None,
            )
        )
        logger.info(
            "tournament_no_show_superseded",
            extra={"pairing_id": str(attempt.pairing_id), "match_id": str(attempt.match_id)},
        )

    async def _record(self, attempt: PairingAttempt, winner_id: UUID) -> None:
        """Closes the attempt as a no-show, before the bracket moves.

        `NO_SHOW` rather than `DECISIVE`: nobody won a game, and a
        permanent record that said otherwise would be claiming a
        competitive fact that never happened.

        The compare-and-set on `outcome IS NULL` is what stops two workers
        both closing it. It runs **before** the advancement for the reason
        every write on this path is ordered that way — a closed attempt with
        no advancement is repaired by the reconciler, and an advancement
        with no closed attempt is a node the sweep would keep re-examining.
        """
        async with self._unit_of_work:
            await self._attempts.complete(
                replace(
                    attempt,
                    status=AttemptStatus.COMPLETED,
                    outcome=AttemptOutcome.NO_SHOW,
                    winner_id=winner_id,
                    completed_at=self._clock.now(),
                )
            )
            await self._unit_of_work.commit()


class _Verdict(StrEnum):
    """What one claimed attempt turned out to be — a closed set, so the
    counters below cannot drift from the cases above."""

    WALKOVER = "walkover"
    ABANDONED = "abandoned"
    PLAYING = "playing"
    SUPERSEDED = "superseded"
    SKIPPED = "skipped"


_NOTHING = NoShowOutcome(claimed=0, walkovers=0, abandoned=0, playing=0, superseded=0)


__all__ = ["NoShowOutcome", "TournamentNoShowService"]
