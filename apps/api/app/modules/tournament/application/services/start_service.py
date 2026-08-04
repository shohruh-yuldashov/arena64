"""Starting a tournament — SPEC-TOURNAMENT §5, A64-019.5.

The moment a bracket stops being a plan and becomes games two people are
expected to play.

    materialise (idempotent, its own transaction)
      -> lock -> REGISTRATION_CLOSED? -> IN_PROGRESS
      -> round one IN_PROGRESS
      -> one `game` match per node that needs one
      -> announce

## Why materialisation is a separate transaction

`TournamentBracketService.materialise` takes its own lock and commits, and
composing it rather than reimplementing it is what keeps one bracket-writing
path. The cost is that a start is two transactions; the cost is affordable
because **both halves are idempotent**, so a worker that dies between them
leaves a materialised bracket that the retry reuses rather than a partial
one nothing can repair.

The order is the reverse of the obvious one for the same reason the launcher
creates the match before recording the attempt: the recoverable state is the
one where more exists than has been recorded.

## Byes create no match, and that is the assertion worth making

A bye is an empty bracket slot (§6a) — never a fake player, never a match.
`materialise` already resolved every one of them to a winner before this
runs, so the nodes this launches for are exactly those with two
participants and no winner. A launcher that filtered on "round one" instead
would create a match for a player with no opponent.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.tournament.application.ports import (
    BracketRepository,
    PairingAttemptRepository,
    RoundRepository,
    TournamentNotFound,
    TournamentNotStartable,
    TournamentRepository,
)
from app.modules.tournament.application.services.bracket_service import (
    TournamentBracketService,
)
from app.modules.tournament.application.services.match_launcher import (
    TournamentMatchLauncher,
    plans_for,
)
from app.modules.tournament.domain.attempts import PairingAttempt
from app.modules.tournament.domain.bracket_plan import FIRST_ROUND
from app.modules.tournament.domain.events import TournamentStarted
from app.modules.tournament.domain.rounds import RoundStatus
from app.modules.tournament.domain.tournament import Tournament, TournamentStatus
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


class TournamentStartService:
    """Moves a closed tournament into play and launches its first round."""

    def __init__(
        self,
        *,
        tournaments: TournamentRepository,
        brackets: TournamentBracketService,
        bracket: BracketRepository,
        rounds: RoundRepository,
        attempts: PairingAttemptRepository,
        launcher: TournamentMatchLauncher,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._tournaments = tournaments
        self._brackets = brackets
        self._bracket = bracket
        self._rounds = rounds
        self._attempts = attempts
        self._launcher = launcher
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def start_tournament(self, tournament_id: UUID) -> list[PairingAttempt]:
        """Starts a tournament and returns the attempts it launched.

        **Idempotent.** A second call finds the tournament already
        `IN_PROGRESS` and launches only what is missing, which is how a
        worker that died mid-launch is repaired without a reconciler having
        to notice. The guarantees are the database's:
        `unique (pairing_id, attempt_number)` and `game`'s own key on the
        derived pairing id.
        """
        tournament = await self._tournaments.by_id(tournament_id)
        if tournament is None:
            raise TournamentNotFound(f"no tournament {tournament_id}")
        self._require_startable(tournament)

        if tournament.status is TournamentStatus.REGISTRATION_CLOSED:
            # Idempotent and its own transaction — see this module's
            # docstring on why the two halves are not one.
            await self._brackets.materialise(tournament_id)

        async with self._unit_of_work:
            locked = await self._tournaments.lock(tournament_id)
            if locked is None:
                raise TournamentNotFound(f"no tournament {tournament_id}")
            self._require_startable(locked)

            started = await self._begin(locked)
            launched = await self._launch_first_round(started)
            await self._unit_of_work.commit()

        logger.info(
            "tournament_started",
            extra={"tournament_id": str(tournament_id), "matches": len(launched)},
        )
        return launched

    async def _begin(self, tournament: Tournament) -> Tournament:
        """`REGISTRATION_CLOSED` → `IN_PROGRESS`, once.

        A tournament already in progress is returned unchanged rather than
        transitioned again: the aggregate would refuse the move, and a
        refusal here would turn a harmless retry into a failure.
        """
        if tournament.status is TournamentStatus.IN_PROGRESS:
            return tournament

        started = tournament.transitioned_to(TournamentStatus.IN_PROGRESS, at=self._clock.now())
        await self._tournaments.save(started)
        await self._start_round(started, FIRST_ROUND)
        await self._events.publish(
            TournamentStarted(occurred_at=self._clock.now(), tournament_id=started.id)
        )
        return started

    async def _start_round(self, tournament: Tournament, round_number: int) -> None:
        """`PUBLISHED` → `IN_PROGRESS` for one round.

        Publication is when players can read a pairing; starting is when
        the matches exist (§6b). A round that is already in progress is
        left alone, so this is safe on a retry.
        """
        rounds = await self._rounds.rounds_for(tournament.id)
        current = next((r for r in rounds if r.round_number == round_number), None)
        if current is None or current.status is not RoundStatus.PUBLISHED:
            return

        await self._rounds.save(current.started(self._clock.now()))

    async def _launch_first_round(self, tournament: Tournament) -> list[PairingAttempt]:
        """One `game` match per round-one node that needs one.

        Byes are skipped because `needs_a_match` is false for them — they
        were resolved to a winner by materialisation, and creating a match
        for a player with no opponent is the failure §6a's "a bye is an
        empty slot" exists to prevent.
        """
        nodes = await self._bracket.nodes_for(tournament.id)
        first_round = [node for node in nodes if node.round_number == FIRST_ROUND]
        return await self._launcher.launch(tournament, plans_for(first_round))

    def _require_startable(self, tournament: Tournament) -> None:
        """§5 — a tournament starts from a closed registration, and only once.

        `IN_PROGRESS` is permitted rather than refused, because this method
        is the retry: the caller's second attempt must be able to finish
        what the first one began.
        """
        if tournament.status in (
            TournamentStatus.REGISTRATION_CLOSED,
            TournamentStatus.IN_PROGRESS,
        ):
            return
        raise TournamentNotStartable(
            f"a tournament starts once registration has closed; this one is "
            f"{tournament.status.value}"
        )


__all__ = ["TournamentStartService"]
