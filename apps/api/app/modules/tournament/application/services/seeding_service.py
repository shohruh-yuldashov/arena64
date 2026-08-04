"""`TournamentSeedingService` — SPEC-TOURNAMENT §6, A64-019.3 §11.

One operation, and its whole value is that running it twice produces the
same bracket.

    lock  ->  registration closed?  ->  active entrants
          ->  ratings, in one batch ->  seed  ->  bracket size
          ->  first-round plan      ->  persist atomically  ->  announce

## Idempotency is the primary key, not a flag

A retry reads the persisted plan and returns it. Two workers racing both
compute a plan — the computation is pure and identical — and the primary
key `(tournament, round, slot)` lets exactly one insert; the loser re-reads
the winner's. There is no marker to set and no lock to hold across the
arithmetic.

That works only because seeding is **deterministic**: if two workers could
produce different plans, the loser re-reading would silently accept a
bracket it did not compute. The third sort key is what makes them identical.

## Why ratings are read once, into values

§3, and the failure it prevents is not performance. Seeding reads a rating
*at a moment*; a per-player query spreads that moment across the field, so
a rating that moves mid-seed could place one player above somebody they
should be below. One batch is one instant.

The values are then persisted as seed numbers (§4), so no later phase ever
consults a live rating to explain a bracket.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.rating.public import SpeedClass
from app.modules.tournament.application.ports import (
    NotSeedable,
    PairingRepository,
    PlanAlreadyExists,
    RatingSnapshots,
    SeedRepository,
    TournamentNotFound,
    TournamentRepository,
)
from app.modules.tournament.domain.events import RoundPublished
from app.modules.tournament.domain.seeding import (
    PlannedPairing,
    SeedInput,
    first_round_pairings,
    seeded,
)
from app.modules.tournament.domain.tournament import Tournament, TournamentStatus
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)

#: The round a seeding produces. Later rounds are A64-019.4's.
FIRST_ROUND = 1


class TournamentSeedingService:
    """Seeds a closed tournament and plans its first round."""

    def __init__(
        self,
        *,
        tournaments: TournamentRepository,
        seeds: SeedRepository,
        pairings: PairingRepository,
        ratings: RatingSnapshots,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._tournaments = tournaments
        self._seeds = seeds
        self._pairings = pairings
        self._ratings = ratings
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def seed_tournament(self, tournament_id: UUID) -> list[PlannedPairing]:
        """Seeds and plans round one. **Idempotent** — §11.

        A second call returns the persisted plan unchanged, whether it was
        written by this caller a moment ago or by another worker mid-race.
        """
        async with self._unit_of_work:
            tournament = await self._tournaments.lock(tournament_id)
            if tournament is None:
                raise TournamentNotFound(f"no tournament {tournament_id}")

            existing = await self._pairings.plan_for(tournament_id, round_number=FIRST_ROUND)
            if existing:
                await self._unit_of_work.commit()
                return existing

            self._require_seedable(tournament)
            plan = await self._planned(tournament)

            try:
                stored = await self._pairings.save_plan(tournament_id, plan)
            except PlanAlreadyExists:
                # Another worker won the race. Its plan is identical —
                # seeding is deterministic — so re-reading is not accepting
                # somebody else's answer, it is reading the same one.
                stored = await self._pairings.plan_for(tournament_id, round_number=FIRST_ROUND)
                await self._unit_of_work.commit()
                return stored

            await self._events.publish(
                RoundPublished(
                    occurred_at=self._clock.now(),
                    tournament_id=tournament_id,
                    round_number=FIRST_ROUND,
                )
            )
            await self._unit_of_work.commit()

        logger.info(
            "tournament_seeded",
            extra={"tournament_id": str(tournament_id), "slots": len(stored)},
        )
        return stored

    def _require_seedable(self, tournament: Tournament) -> None:
        """§2 — registration must be closed first.

        Seeding an open tournament would build a bracket from a field that
        can still change, and the plan is immutable once written. Refused
        rather than tolerated, because the failure is invisible: the bracket
        would simply be missing whoever registered next.
        """
        if tournament.status is not TournamentStatus.REGISTRATION_CLOSED:
            raise NotSeedable(
                f"a tournament is seeded once registration has closed; "
                f"this one is {tournament.status.value}"
            )

    async def _planned(self, tournament: Tournament) -> list[PlannedPairing]:
        """The first round, computed and persisted as seed numbers."""
        entrants = await self._seeds.active_entrants(tournament.id)

        # One batch, one instant — see this module's docstring on why that
        # is correctness rather than speed.
        snapshots = await self._ratings.ratings_for(
            entrants,
            variant=tournament.variant,
            speed_class=SpeedClass.CLASSICAL,
        )

        seeds = seeded(
            [
                SeedInput(
                    player_id=player_id,
                    rating=snapshots[player_id].value,
                    deviation=snapshots[player_id].deviation,
                    is_provisional=snapshots[player_id].is_provisional,
                )
                for player_id in entrants
            ]
        )
        await self._seeds.assign(tournament.id, seeds)

        return first_round_pairings(seeds, round_number=FIRST_ROUND)


__all__ = ["FIRST_ROUND", "TournamentSeedingService"]
