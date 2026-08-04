"""Materialising a bracket and moving winners through it. §5, §7, §10.

Two operations, one transaction each.

## Materialisation is all or nothing

§10. One transaction writes every round, every node and every bye already
resolved, so a partial bracket is impossible. A tournament with three of its
seven nodes would be one nothing can walk and nothing can repair — there is
no "resume materialisation" and there should not be, because the
computation is cheap and deterministic.

Idempotent by the primary key: a second attempt collides and re-reads.

## Advancement is a compare-and-set, and the loser re-reads

§8's race is two workers processing one completed match.
`UPDATE … WHERE winner_id IS NULL` lets exactly one write. The loser reads
the stored winner and, if it agrees, returns — the work was done. If it
disagrees, that is a genuine conflict and it says so rather than
overwriting, because on a bracket an overwrite means a player advancing out
of a node they lost.

Propagation runs after every advancement, because filling a parent may
leave *it* with one participant — a bye chain that the domain resolves to a
fixed point.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.tournament.application.ports import (
    BracketRepository,
    NotSeedable,
    PairingRepository,
    PlanAlreadyExists,
    SeedRepository,
    TournamentNotFound,
    TournamentRepository,
)
from app.modules.tournament.domain.bracket_plan import (
    FIRST_ROUND,
    BracketSlot,
    bracket_for,
    propagated,
    round_count,
)
from app.modules.tournament.domain.events import RoundPublished
from app.modules.tournament.domain.exceptions import InvalidBracketPosition
from app.modules.tournament.domain.rounds import TournamentRound
from app.modules.tournament.domain.seeding import bracket_size
from app.modules.tournament.domain.tournament import TournamentStatus
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


class ConflictingWinner(InvalidBracketPosition):
    """This node already has a *different* winner — §7.

    Distinct from a duplicate: an identical advancement is idempotent
    success, and this is the case where two results disagree about who won
    one node. Refused rather than resolved, because nothing here can know
    which is right.
    """


class TournamentBracketService:
    """Materialises a bracket, then advances winners through it."""

    def __init__(
        self,
        *,
        tournaments: TournamentRepository,
        seeds: SeedRepository,
        pairings: PairingRepository,
        bracket: BracketRepository,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._tournaments = tournaments
        self._seeds = seeds
        self._pairings = pairings
        self._bracket = bracket
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def materialise(self, tournament_id: UUID) -> list[BracketSlot]:
        """Builds the whole tree, byes resolved. Idempotent — §5.

        Round one is created **published**: its participants are known the
        moment the bracket exists, and publication is what freezes them.
        Later rounds are created `PENDING` — their slots are still filling,
        and A64-019.5 publishes each when its participants are known.
        """
        async with self._unit_of_work:
            tournament = await self._tournaments.lock(tournament_id)
            if tournament is None:
                raise TournamentNotFound(f"no tournament {tournament_id}")
            if tournament.status is not TournamentStatus.REGISTRATION_CLOSED:
                raise NotSeedable(
                    f"a bracket is built once registration has closed; "
                    f"this tournament is {tournament.status.value}"
                )

            if await self._bracket.exists(tournament_id):
                existing = await self._bracket.nodes_for(tournament_id)
                await self._unit_of_work.commit()
                return existing

            seeds = await self._seeds.seeds_for(tournament_id)
            if not seeds:
                raise NotSeedable("this tournament has not been seeded")

            first_round = await self._pairings.plan_for(tournament_id, round_number=FIRST_ROUND)
            nodes = bracket_for(seeds, first_round)
            rounds = self._rounds_for(len(seeds))

            try:
                await self._bracket.materialise(tournament_id, nodes, rounds)
            except PlanAlreadyExists:
                # Another worker won. Its tree is identical — the whole
                # computation is deterministic — so re-reading is reading
                # the same answer, not accepting somebody else's.
                nodes = await self._bracket.nodes_for(tournament_id)
                await self._unit_of_work.commit()
                return nodes

            # Round one's slots came from seeding, so its byes are resolved
            # here rather than at insert time — the propagation pass writes
            # exactly the difference between what is stored and the fixed
            # point the domain computed.
            await self._propagate(tournament_id)

            await self._events.publish(
                RoundPublished(
                    occurred_at=self._clock.now(),
                    tournament_id=tournament_id,
                    round_number=FIRST_ROUND,
                )
            )
            nodes = await self._bracket.nodes_for(tournament_id)
            await self._unit_of_work.commit()

        logger.info(
            "tournament_bracket_materialised",
            extra={"tournament_id": str(tournament_id), "nodes": len(nodes)},
        )
        return nodes

    async def advance_winner(
        self, tournament_id: UUID, *, round_number: int, slot: int, winner_id: UUID
    ) -> list[BracketSlot]:
        """Records a node's winner and propagates. Idempotent — §7, §8.

        A repeated identical advancement returns the bracket unchanged; a
        *different* winner raises `ConflictingWinner`. Nothing here decides
        between two disagreeing results — that is a moderation question the
        Administration epic owns.
        """
        async with self._unit_of_work:
            await self._tournaments.lock(tournament_id)
            nodes = {
                (node.round_number, node.slot): node
                for node in await self._bracket.nodes_for(tournament_id)
            }
            node = nodes.get((round_number, slot))
            if node is None:
                raise InvalidBracketPosition(f"no node at round {round_number} slot {slot}")
            if winner_id not in node.participants:
                raise InvalidBracketPosition("the winner of a node must be one of its participants")

            claimed = await self._bracket.claim_winner(
                tournament_id, round_number=round_number, slot=slot, winner_id=winner_id
            )
            if not claimed:
                # Somebody was here first — agreement or conflict.
                current = await self._bracket.nodes_for(tournament_id)
                stored = next(
                    n for n in current if (n.round_number, n.slot) == (round_number, slot)
                )
                if stored.winner_id != winner_id:
                    raise ConflictingWinner(
                        f"round {round_number} slot {slot} was already won by another player"
                    )
                await self._unit_of_work.commit()
                return current

            decided = node.with_winner(winner_id)
            parent_coordinate = decided.parent()
            if parent_coordinate in nodes:
                await self._bracket.fill_seat(
                    tournament_id,
                    round_number=parent_coordinate[0],
                    slot=parent_coordinate[1],
                    player_id=winner_id,
                    seed=decided.seed_of(winner_id),
                    light=decided.takes_light_seat_of_parent(),
                )

            await self._propagate(tournament_id)
            await self._unit_of_work.commit()

        return await self._bracket.nodes_for(tournament_id)

    async def _propagate(self, tournament_id: UUID) -> None:
        """Resolves any bye the last advancement created — §6.

        Filling a parent can leave it with one participant and no opponent
        coming, which is a bye that must be taken now rather than waiting
        for a match that will never be created. The domain computes the
        fixed point; this writes the difference.
        """
        stored = {
            (node.round_number, node.slot): node
            for node in await self._bracket.nodes_for(tournament_id)
        }
        for resolved in propagated(list(stored.values())):
            before = stored[(resolved.round_number, resolved.slot)]
            if resolved.winner_id and not before.winner_id:
                await self._bracket.claim_winner(
                    tournament_id,
                    round_number=resolved.round_number,
                    slot=resolved.slot,
                    winner_id=resolved.winner_id,
                )
            for light in (True, False):
                new = resolved.light_player_id if light else resolved.dark_player_id
                old = before.light_player_id if light else before.dark_player_id
                if new and not old:
                    await self._bracket.fill_seat(
                        tournament_id,
                        round_number=resolved.round_number,
                        slot=resolved.slot,
                        player_id=new,
                        seed=resolved.light_seed if light else resolved.dark_seed,
                        light=light,
                    )

    def _rounds_for(self, entrant_count: int) -> list[TournamentRound]:
        """One `TournamentRound` per layer, round one already published.

        Built from the domain aggregate rather than assembled here, so the
        lifecycle rule lives in one place — a repository that set statuses
        directly would be a second copy of the state machine.
        """
        total = round_count(bracket_size(entrant_count))
        rounds = [
            TournamentRound(tournament_id=UUID(int=0), round_number=number)
            for number in range(FIRST_ROUND, total + 1)
        ]
        rounds[0] = rounds[0].published(self._clock.now())
        return rounds


__all__ = ["ConflictingWinner", "TournamentBracketService"]
