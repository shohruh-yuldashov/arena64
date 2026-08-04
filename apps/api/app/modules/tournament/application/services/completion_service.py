"""Finishing a tournament — SPEC-TOURNAMENT §6f, A64-019.6.

The moment a bracket stops being a live thing and becomes a record.

    lock -> IN_PROGRESS? -> the final has a winner?
         -> every pairing settled?
         -> derive placement -> materialise standings
         -> COMPLETED -> announce
         -> commit

## One transaction, and the partial state it forbids

Everything above happens in a single unit of work, because the state the
brief names as impossible — **`COMPLETED` with no standings** — is a
tournament whose result page is empty and whose bracket says it is over.
Nothing could repair it: the completion path would refuse to run again
(the status is terminal), and the derivation it would need is gone.

So the standings are written *before* the transition, in the same
transaction. If the write fails, nothing is committed and the tournament is
still `IN_PROGRESS` — which the next completed match, or the reconciler,
reaches again.

This is deliberately the opposite of A64-019.5's "effects first, bookkeeping
last". There the recoverable state was "more happened than was recorded";
here there is no *later* to recover in, because `COMPLETED` is terminal.

## Idempotency is two database facts, never a flag

    pk_standing                  one result row per (tournament, player)
    uq_standing__one_champion    one rank-1 player per tournament
    Tournament.transitioned_to   COMPLETED is terminal

A second completion finds the tournament already `COMPLETED` and returns the
stored standings unchanged. Two workers racing both derive an identical
placement — the derivation is pure over a bracket that can no longer move —
so the loser re-reading is reading its own answer rather than accepting
somebody else's, which is the same argument seeding and materialisation make.

## The results are a snapshot, not a view

`standings_for` runs **once**. A read endpoint pages over stored rows and
never re-derives, because a derivation that ran per request would make a
published result depend on code that can change — and the bracket it came
from is already terminal, so there is nothing a recomputation could learn.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.tournament.application.ports import (
    BracketRepository,
    PairingAttemptRepository,
    SeedRepository,
    StandingRepository,
    StandingsAlreadyRecorded,
    TournamentNotFound,
    TournamentRepository,
)
from app.modules.tournament.domain.bracket_plan import BracketSlot
from app.modules.tournament.domain.events import TournamentCompleted
from app.modules.tournament.domain.exceptions import InvalidBracketPosition
from app.modules.tournament.domain.standings import (
    FIRST_PLACE,
    Standing,
    standings_for,
)
from app.modules.tournament.domain.tournament import Tournament, TournamentStatus
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


class TournamentNotFinished(InvalidBracketPosition):
    """The bracket is not decided, so there is nothing to record.

    Raised when the final has no winner or a pairing is still unsettled.
    **Not a failure to retry blindly**: the caller is either the advancement
    flow, which reaches it again on the next completed match, or an operator
    asking too early.
    """


class TournamentCompletionService:
    """Materialises a tournament's final result, once."""

    def __init__(
        self,
        *,
        tournaments: TournamentRepository,
        bracket: BracketRepository,
        attempts: PairingAttemptRepository,
        seeds: SeedRepository,
        standings: StandingRepository,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._tournaments = tournaments
        self._bracket = bracket
        self._attempts = attempts
        self._seeds = seeds
        self._standings = standings
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def complete_tournament(self, tournament_id: UUID) -> list[Standing]:
        """Completes a tournament and returns its immutable standings.

        **Idempotent.** A second call returns the stored result unchanged —
        same ranks, same statistics, no second event. Raises
        `TournamentNotFinished` when the bracket is not decided, which is
        the ordinary answer while a round is still being played.
        """
        async with self._unit_of_work:
            tournament = await self._tournaments.lock(tournament_id)
            if tournament is None:
                raise TournamentNotFound(f"no tournament {tournament_id}")

            if tournament.status is TournamentStatus.COMPLETED:
                # Already finished. Read the stored answer rather than
                # deriving a second one: the rows are the record, and a
                # fresh derivation is at best identical and at worst a
                # different published result from the same bracket.
                stored = await self._standings.standings_for(tournament_id)
                await self._unit_of_work.commit()
                return stored

            if tournament.status is not TournamentStatus.IN_PROGRESS:
                raise TournamentNotFinished(
                    f"a tournament is completed from play; this one is {tournament.status.value}"
                )

            nodes = await self._bracket.nodes_for(tournament_id)
            self._require_decided(nodes)

            recorded = await self._materialise(tournament, nodes)
            await self._unit_of_work.commit()

        logger.info(
            "tournament_results_materialised",
            extra={
                "tournament_id": str(tournament_id),
                "entrants": len(recorded),
                "winner_id": str(recorded[0].player_id) if recorded else None,
            },
        )
        return recorded

    async def _materialise(
        self, tournament: Tournament, nodes: list[BracketSlot]
    ) -> list[Standing]:
        """Derives, writes, transitions and announces — all inside the
        caller's transaction.

        The order within it is deliberate: the **standings first**, so a
        failure leaves a tournament that is still `IN_PROGRESS` and will be
        completed again, rather than one that is `COMPLETED` with no result.
        """
        seeds = {
            seed.player_id: seed.seed_number for seed in await self._seeds.seeds_for(tournament.id)
        }
        attempts = await self._attempts.for_pairings(
            [node.id for node in nodes if node.id is not None]
        )

        derived = standings_for(
            tournament.id,
            nodes=nodes,
            attempts=attempts,
            seeds=seeds,
            at=self._clock.now(),
        )

        try:
            await self._standings.record(derived)
        except StandingsAlreadyRecorded:
            # Another worker materialised between this one's lock and its
            # write, which the row lock should make unreachable and which is
            # handled anyway. Its rows are identical — the derivation is pure
            # over a terminal bracket — so re-reading is reading this call's
            # own answer.
            return await self._standings.standings_for(tournament.id)

        completed = tournament.transitioned_to(TournamentStatus.COMPLETED, at=self._clock.now())
        await self._tournaments.save(completed)
        await self._events.publish(
            TournamentCompleted(
                occurred_at=self._clock.now(),
                tournament_id=tournament.id,
                winner_id=_champion_of(derived),
                entrant_count=len(derived),
            )
        )
        return derived

    def _require_decided(self, nodes: list[BracketSlot]) -> None:
        """Every node settled, and the final won — §5's steps 3 and 4.

        Both, not just the final: a bye chain can decide a final while a
        node beneath it is still being played, and standings derived then
        would record an elimination that had not happened.

        A node with fewer than two participants and no winner is **not**
        unsettled — that is an empty subtree a bracket with byes legitimately
        has, and demanding a winner for one would make every such tournament
        permanently unfinishable.
        """
        if not nodes:
            raise TournamentNotFinished("this tournament has no bracket")

        unsettled = [
            node for node in nodes if node.winner_id is None and len(node.participants) == 2
        ]
        if unsettled:
            raise TournamentNotFinished(f"{len(unsettled)} pairing(s) are still being played")

        final_round = max(node.round_number for node in nodes)
        final = next(node for node in nodes if node.round_number == final_round)
        if final.winner_id is None:
            raise TournamentNotFinished("the final has no winner")


def _champion_of(standings: list[Standing]) -> UUID:
    """The rank-1 player. Present by construction — `standings_for` derives
    placement from a final that has a winner, and the relation's own partial
    unique index admits exactly one."""
    champion = next((s for s in standings if s.final_rank == FIRST_PLACE), None)
    if champion is None:  # pragma: no cover — the derivation always names one
        raise TournamentNotFinished("no champion was derived")
    return champion.player_id


__all__ = ["TournamentCompletionService", "TournamentNotFinished"]
