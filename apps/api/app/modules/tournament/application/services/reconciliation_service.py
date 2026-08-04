"""`TournamentReconciliationService` — the recovery A64-019.5 owes.

Launching a tournament match is two writes that cannot share a transaction:
`game` commits a match, then this module records the attempt. services.md
BE-05 forbids collapsing them — a cross-context call inside an open
transaction holds row locks across another module's work — so there is a
window in which one exists and the other does not. Every other recovery
mechanism on this platform closes exactly this shape of window, and this is
`tournament`'s.

## What it derives, and from what

Nothing here remembers what a dead worker intended. It claims a bounded page
of running tournaments, asks `game` what became of the matches it created
for their nodes (`OriginMatchReader`, keyed by R-25's `origin_ref`), and
compares that against its own attempts:

    node needs a match, `game` has none      launch it
    `game` has a match, no attempt row       record the attempt
    match decided or drawn, nothing followed apply it to the bracket
    attempt names a match `game` has lost    report — see below
    match ended with no result at all        report — see below

The first three are repairs. The last two are **reported and not repaired**,
and that is a decision rather than an omission: both mean a node whose match
will never produce a result, and who advances then is SPEC-TOURNAMENT OQ-2 —
undecided, because no-show policy waits on the Administration epic. Guessing
would write a permanent competitive record nobody chose. They are counted
and logged at `ERROR` so an operator sees a stuck bracket rather than
discovering it from a player.

## Why every repair is safe to run twice

    launch      `game`'s unique key on the derived pairing id returns the
                match an earlier call created, and
                `unique (pairing_id, attempt_number)` refuses a second row
    record      the same unique key
    apply       `TournamentAdvancementService` is idempotent end to end —
                its compare-and-set on `winner_id IS NULL` is what decides

So a second worker reaching the same tournament does the same work to no
effect, and a redelivered task claims a page that is already clean.

## Never raises

It runs from a schedule, and a sweep that propagated would stop the schedule
that called it — `ClockAdjudicationService.adjudicate_once`'s argument, and
the same posture. A failure on one tournament must not cost the others their
tick either, so the guard is per tournament as well as around the sweep.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid5

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
)
from app.modules.tournament.application.services.match_launcher import (
    PlannedAttempt,
    TournamentMatchLauncher,
)
from app.modules.tournament.domain.attempts import (
    FIRST_ATTEMPT,
    MAX_ATTEMPTS,
    AttemptOutcome,
    AttemptStatus,
    PairingAttempt,
    rematch_seats,
)
from app.modules.tournament.domain.bracket_plan import BracketSlot
from app.modules.tournament.domain.tournament import Tournament

logger = logging.getLogger(__name__)

#: How many tournaments one tick examines.
#:
#: A ceiling rather than a target: an unbounded sweep is an outage waiting
#: for enough tournaments, and the claim skips whatever another worker
#: already holds, so a backlog drains across ticks rather than in one.
DEFAULT_BATCH_SIZE = 20


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """What one `reconcile_once` did.

    Returned rather than only logged — the shape `ExpirySweep` and
    `PairingOutcome` already use: a test asserts on the outcome and the
    worker logs it once.

    The counters are separated because an operator acts differently on
    each. A steady trickle of `launched` means workers are dying before
    reaching `game`; of `recorded`, that they are dying just after. Either
    `orphaned` or `abandoned` above zero is a **stuck bracket**, and no
    amount of running this again will move it.
    """

    scanned: int
    """Tournaments this tick claimed."""

    launched: int
    """Nodes that were owed a match and had none."""

    recorded: int
    """Matches `game` had that this module had not written down."""

    advanced: int
    """Finished matches whose consequence had not been applied."""

    orphaned: int
    """Attempts naming a match `game` no longer has. Not repaired."""

    abandoned: int
    """Matches that ended with no result — declined, expired, aborted.
    Not repaired: who advances is OQ-2."""

    @property
    def repaired(self) -> int:
        return self.launched + self.recorded + self.advanced

    @property
    def stuck(self) -> int:
        return self.orphaned + self.abandoned


class TournamentReconciliationService:
    """One bounded, idempotent reconciliation pass."""

    def __init__(
        self,
        *,
        tournaments: TournamentRepository,
        bracket: BracketRepository,
        attempts: PairingAttemptRepository,
        origin_matches: OriginMatchReader,
        launcher: TournamentMatchLauncher,
        advancement: TournamentAdvancementService,
        unit_of_work: UnitOfWork,
        clock: Clock,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._tournaments = tournaments
        self._bracket = bracket
        self._attempts = attempts
        self._origin_matches = origin_matches
        self._launcher = launcher
        self._advancement = advancement
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._batch_size = batch_size

    async def reconcile_once(self) -> ReconciliationOutcome:
        """One tick. Never raises — see this module's docstring."""
        try:
            claimed = await self._claim()
        except Exception as exc:  # noqa: BLE001 — a sweep must not stop its schedule
            logger.error(
                "tournament_reconciliation_claim_failed",
                extra={"error": type(exc).__name__},
                exc_info=exc,
            )
            return _NOTHING

        totals = [_NOTHING]
        for tournament_id in claimed:
            totals.append(await self._reconcile_one(tournament_id))

        outcome = _summed(totals, scanned=len(claimed))
        if outcome.repaired or outcome.stuck:
            logger.info(
                "tournament_reconciliation_tick",
                extra={
                    "scanned": outcome.scanned,
                    "launched": outcome.launched,
                    "recorded": outcome.recorded,
                    "advanced": outcome.advanced,
                    "orphaned": outcome.orphaned,
                    "abandoned": outcome.abandoned,
                },
            )
        return outcome

    async def _claim(self) -> list[UUID]:
        """A bounded page of running tournaments, claimed for this worker.

        Its own transaction, so the rows this worker took are visibly locked
        before anything else happens — `QueueService.expire_due`'s argument,
        and `SKIP LOCKED` is what makes a second reconciler skip rather than
        wait.
        """
        async with self._unit_of_work:
            claimed = await self._tournaments.in_progress(limit=self._batch_size)
            await self._unit_of_work.commit()
        return claimed

    async def _reconcile_one(self, tournament_id: UUID) -> ReconciliationOutcome:
        """One tournament's drift. Failures are contained here.

        A tournament that cannot be read must not cost the rest of the page
        its tick — the same reason the batch loop in every outbox consumer
        catches per entry.
        """
        try:
            return await self._repair(tournament_id)
        except Exception as exc:  # noqa: BLE001 — one tournament must not stop a page
            logger.error(
                "tournament_reconciliation_failed",
                extra={"tournament_id": str(tournament_id), "error": type(exc).__name__},
                exc_info=exc,
            )
            return _NOTHING

    async def _repair(self, tournament_id: UUID) -> ReconciliationOutcome:
        tournament = await self._tournaments.by_id(tournament_id)
        if tournament is None:  # pragma: no cover — it was claimed a moment ago
            return _NOTHING

        # Only nodes that are still waiting on a result. A decided node is
        # settled whatever its matches say, and a node whose seats are not
        # both filled is waiting for the round beneath it rather than for a
        # match — launching there is the phantom advancement §6b forbids.
        pending = [
            node for node in await self._bracket.nodes_for(tournament_id) if node.needs_a_match
        ]
        references = [node.id for node in pending if node.id is not None]
        if not references:
            return _NOTHING

        attempts = await self._attempts.for_pairings(references)
        outcomes = await self._origin_matches.outcomes_for(
            references, origin=MatchOrigin.TOURNAMENT
        )

        by_node = {node.id: node for node in pending}
        totals = [_NOTHING]
        for reference in references:
            node = by_node[reference]
            totals.append(
                await self._repair_node(
                    tournament,
                    node,
                    attempts=[a for a in attempts if a.pairing_id == reference],
                    outcomes=sorted(
                        (o for o in outcomes if o.origin_ref == reference),
                        key=lambda outcome: outcome.created_at,
                    ),
                )
            )
        return _summed(totals, scanned=0)

    async def _repair_node(
        self,
        tournament: Tournament,
        node: BracketSlot,
        *,
        attempts: list[PairingAttempt],
        outcomes: list[OriginMatchOutcome],
    ) -> ReconciliationOutcome:
        """One node's drift, in the order the states can be repaired.

        Recording comes before applying, because an attempt that does not
        exist cannot be advanced — and the same tick should finish the job
        rather than leaving half of it for the next one.
        """
        recorded = await self._record_missing(node, attempts=attempts, outcomes=outcomes)
        attempts = attempts + recorded

        if not attempts and not outcomes:
            launched = await self._launch_missing(tournament, node)
            return ReconciliationOutcome(
                scanned=0, launched=launched, recorded=0, advanced=0, orphaned=0, abandoned=0
            )

        by_match = {attempt.match_id: attempt for attempt in attempts}
        latest = max(attempts, key=lambda attempt: attempt.attempt_number) if attempts else None

        advanced = abandoned = 0
        for outcome in outcomes:
            attempt = by_match.get(outcome.match_id)
            if attempt is None:  # pragma: no cover — `_record_missing` wrote it
                continue
            if outcome.state is OriginMatchState.ABANDONED:
                abandoned += 1
                logger.error(
                    "tournament_match_abandoned",
                    extra={
                        "tournament_id": str(tournament.id),
                        "pairing_id": str(attempt.pairing_id),
                        "match_id": str(outcome.match_id),
                    },
                )
                continue
            if latest is not None and _needs_applying(node, attempt, latest, outcome):
                await self._apply(attempt, outcome)
                advanced += 1

        orphaned = _orphaned(attempts, outcomes)
        if orphaned:
            logger.error(
                "tournament_attempt_orphaned",
                extra={"tournament_id": str(tournament.id), "pairing_id": str(node.id)},
            )

        return ReconciliationOutcome(
            scanned=0,
            launched=0,
            recorded=len(recorded),
            advanced=advanced,
            orphaned=orphaned,
            abandoned=abandoned,
        )

    async def _launch_missing(self, tournament: Tournament, node: BracketSlot) -> int:
        """A node owed a match and having none — states one and four.

        One method for both, because they are the same repair: the node's
        seats and the first attempt number, launched through the same
        idempotent path a start uses.
        """
        if node.id is None:  # pragma: no cover — filtered by the caller
            return 0

        async with self._unit_of_work:
            await self._launcher.launch(
                tournament,
                [
                    PlannedAttempt(
                        pairing_id=node.id,
                        attempt_number=FIRST_ATTEMPT,
                        light_player_id=node.participants[0],
                        dark_player_id=node.participants[1],
                    )
                ],
            )
            await self._unit_of_work.commit()

        logger.warning(
            "tournament_match_relaunched",
            extra={"tournament_id": str(tournament.id), "pairing_id": str(node.id)},
        )
        return 1

    async def _record_missing(
        self,
        node: BracketSlot,
        *,
        attempts: list[PairingAttempt],
        outcomes: list[OriginMatchOutcome],
    ) -> list[PairingAttempt]:
        """Attempt rows for matches `game` has and this module does not.

        The window BE-05 leaves open: the match committed and the worker
        died before writing the row. The numbers are assigned **in creation
        order**, which is why the view carries `created_at` — a rematch is
        by definition the later match of a pairing, and there is nothing
        else that says so.

        The seats are derived rather than read back, because `game` does not
        publish them and the rule that produced them is this module's: the
        node's own seats for the first attempt, swapped for the second
        (§6c).
        """
        known = {attempt.match_id for attempt in attempts}
        unknown = [outcome for outcome in outcomes if outcome.match_id not in known]
        if not unknown or node.id is None:
            return []

        taken = {attempt.attempt_number for attempt in attempts}
        free = [n for n in range(FIRST_ATTEMPT, MAX_ATTEMPTS + 1) if n not in taken]

        written: list[PairingAttempt] = []
        for number, outcome in zip(free, unknown, strict=False):
            light, dark = self._seats_for(node, number, attempts)
            async with self._unit_of_work:
                written.append(
                    await self._attempts.record(
                        PairingAttempt(
                            id=_deterministic_id(node.id, number),
                            pairing_id=node.id,
                            attempt_number=number,
                            match_id=outcome.match_id,
                            light_player_id=light,
                            dark_player_id=dark,
                        )
                    )
                )
                await self._unit_of_work.commit()

            logger.warning(
                "tournament_attempt_recovered",
                extra={"pairing_id": str(node.id), "attempt": number},
            )
        return written

    def _seats_for(
        self, node: BracketSlot, number: int, attempts: list[PairingAttempt]
    ) -> tuple[UUID, UUID]:
        """Who sat where in attempt `number` — §6c's alternation.

        The first attempt takes the node's seats. A rematch swaps whatever
        the attempt before it played, which is the first attempt's seats
        when one is on record and the node's otherwise — the two agree, and
        preferring the record means a future change to the seating rule
        cannot make a recovered row disagree with a launched one.
        """
        first = next((a for a in attempts if a.attempt_number == FIRST_ATTEMPT), None)
        if number == FIRST_ATTEMPT:
            return (node.participants[0], node.participants[1])
        if first is not None:
            return rematch_seats(first)
        return (node.participants[1], node.participants[0])

    async def _apply(self, attempt: PairingAttempt, outcome: OriginMatchOutcome) -> None:
        """Re-runs the consequence a completion should already have had.

        The **same** service the outbox consumer drives, so a repair and a
        delivery cannot disagree about what a result means. Idempotent end
        to end, which is what makes calling it on a maybe-handled match
        safe.
        """
        await self._advancement.apply(
            CompletedTournamentMatch(
                match_id=outcome.match_id,
                pairing_id=attempt.pairing_id,
                outcome=(
                    AttemptOutcome.DECISIVE
                    if outcome.state is OriginMatchState.DECIDED
                    else AttemptOutcome.DRAW
                ),
                winner_seat=outcome.winner.value if outcome.winner is not None else None,
            )
        )
        logger.warning(
            "tournament_advancement_recovered",
            extra={"pairing_id": str(attempt.pairing_id), "match_id": str(outcome.match_id)},
        )


def _needs_applying(
    node: BracketSlot,
    attempt: PairingAttempt,
    latest: PairingAttempt,
    outcome: OriginMatchOutcome,
) -> bool:
    """Whether a finished match's consequence is still missing.

    Two cases, and both are "the result is in `game` and the bracket has not
    acted on it":

        the attempt is still `created`   nothing recorded the result at all
        the attempt is completed but     the result was recorded and what it
        nothing followed from it         implies — an advancement or a
                                         rematch — was not

    The second is recognised by the node having no winner *and* this being
    the newest attempt: an older attempt that drew is answered by the
    rematch above it existing, and a node with a winner is answered.
    """
    if outcome.state not in (OriginMatchState.DECIDED, OriginMatchState.DRAWN):
        return False
    if attempt.status is AttemptStatus.CREATED:
        return True
    return node.winner_id is None and latest.attempt_number == attempt.attempt_number


def _orphaned(attempts: list[PairingAttempt], outcomes: list[OriginMatchOutcome]) -> int:
    """Attempts naming a match `game` does not have.

    Counted, never repaired. `game`'s own retention deletes pairings that
    never became games, so the usual cause is a tournament match nobody
    accepted — and who advances then is OQ-2, which no code here may decide
    on a player's behalf.
    """
    live = {outcome.match_id for outcome in outcomes}
    return sum(1 for attempt in attempts if attempt.match_id not in live)


def _deterministic_id(pairing_id: UUID, number: int) -> UUID:
    """The identity a recovered attempt row takes.

    Derived rather than random, so two reconcilers recovering the same
    attempt at the same instant produce the same row — the unique key then
    refuses the second insert instead of leaving whichever committed first.
    """
    return uuid5(pairing_id, f"recovered-attempt:{number}")


def _summed(parts: Sequence[ReconciliationOutcome], *, scanned: int) -> ReconciliationOutcome:
    return ReconciliationOutcome(
        scanned=scanned + sum(part.scanned for part in parts),
        launched=sum(part.launched for part in parts),
        recorded=sum(part.recorded for part in parts),
        advanced=sum(part.advanced for part in parts),
        orphaned=sum(part.orphaned for part in parts),
        abandoned=sum(part.abandoned for part in parts),
    )


_NOTHING = ReconciliationOutcome(
    scanned=0, launched=0, recorded=0, advanced=0, orphaned=0, abandoned=0
)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "ReconciliationOutcome",
    "TournamentReconciliationService",
]
