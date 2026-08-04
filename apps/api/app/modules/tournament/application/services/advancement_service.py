"""What a completed `game` match does to a bracket — §6c, §6b.

The other half of A64-019.5. `TournamentStartService` launches matches;
this is what happens when one of them ends.

    decide  ->  rematch, or advance
            ->  round complete?  -> publish and launch the next one
            ->  final complete?  -> the tournament has a winner

## Effects first, bookkeeping last

The order every method here follows, and it is the same argument
`TournamentMatchLauncher` makes about creating the match before recording
the attempt: **the recoverable state is the one where more has happened than
has been written down.**

So the rematch is launched, or the winner advanced, *before* the attempt is
marked completed. A worker that dies in between leaves an attempt that still
says `created`, and the redelivery re-derives exactly the same decision from
the same payload — the launch collides with `unique (pairing_id,
attempt_number)` and the advancement collides with `winner_id IS NULL`, so
both are no-ops and the bookkeeping finally lands. Marking the attempt first
would leave the opposite: a bracket that believes the match is handled and
never acts on it.

That is also why `complete` returning `False` is **not** treated as "stop".
It means somebody already recorded this result; it says nothing about
whether the advancement it implies was carried out.

## The decision is `domain/attempts.decide`, and nothing here re-derives it

One function holds the whole draw policy so a caller cannot reach a
different conclusion by taking branches in a different order. This service
supplies its three inputs — the outcome, the winner, and the **higher seed
read from the node** — and applies whatever comes back.

The higher seed comes from the *pairing*, never from the attempt: a rematch
swaps the seats, and a seed read from the seats played would make the
adjudication depend on which game of a tie it was.
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.tournament.application.ports import (
    BracketRepository,
    PairingAttemptRepository,
    RoundRepository,
    TournamentRepository,
)
from app.modules.tournament.application.services.bracket_service import (
    TournamentBracketService,
)
from app.modules.tournament.application.services.match_launcher import (
    PlannedAttempt,
    TournamentMatchLauncher,
    plans_for,
)
from app.modules.tournament.domain.attempts import (
    AdvancementReason,
    AttemptOutcome,
    AttemptStatus,
    PairingAttempt,
    decide,
    rematch_seats,
)
from app.modules.tournament.domain.bracket_plan import BracketSlot, LocatedNode
from app.modules.tournament.domain.events import (
    RoundCompleted,
    RoundPublished,
    TournamentCompleted,
)
from app.modules.tournament.domain.exceptions import InvalidBracketPosition
from app.modules.tournament.domain.rounds import RoundStatus, TournamentRound
from app.modules.tournament.domain.tournament import Tournament, TournamentStatus
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)

#: The two seats, as `game.match_completed` spells them.
#:
#: Plain strings rather than `engine.PlayerSide`, for A64-019.3 §6a's
#: reason: R-2 lets only `game`, `replay` and `fairplay` import the engine,
#: and a tournament decides *which seat* rather than what a side means.
LIGHT_SEAT = "light"
DARK_SEAT = "dark"


@dataclass(frozen=True, slots=True)
class CompletedTournamentMatch:
    """One `game.match_completed` this module recognised as its own.

    Decoded from the payload by the consumer, so this service never sees a
    dictionary and never imports a `game` event type.
    """

    match_id: UUID
    pairing_id: UUID
    """The node the match was created for — `origin_ref`, R-25."""

    outcome: AttemptOutcome
    winner_seat: str | None
    """`"light"`, `"dark"`, or `None` for a draw. See `LIGHT_SEAT`."""


class UnknownAttempt(InvalidBracketPosition):
    """No attempt names this match.

    Not a failure to retry: either the match belongs to another context that
    happens to use the `tournament` origin, or the attempt row was never
    written because a worker died between creating the match and recording
    it. The second is the reconciler's to repair, and retrying the
    completion forever would not help it.
    """


class TournamentAdvancementService:
    """Applies one completed match to the bracket it was played for."""

    def __init__(
        self,
        *,
        tournaments: TournamentRepository,
        bracket: BracketRepository,
        brackets: TournamentBracketService,
        rounds: RoundRepository,
        attempts: PairingAttemptRepository,
        launcher: TournamentMatchLauncher,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._tournaments = tournaments
        self._bracket = bracket
        self._brackets = brackets
        self._rounds = rounds
        self._attempts = attempts
        self._launcher = launcher
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def apply(self, completion: CompletedTournamentMatch) -> None:
        """Records a result and moves the bracket on. Idempotent — §6c.

        Raises `UnknownAttempt` when nothing here was expecting this match,
        which the consumer treats as "not mine" rather than as a failure.
        """
        attempt = await self._attempts.by_match(completion.match_id)
        if attempt is None or attempt.pairing_id != completion.pairing_id:
            raise UnknownAttempt(f"no tournament attempt for match {completion.match_id}")

        located = await self._bracket.locate(attempt.pairing_id)
        if located is None:  # pragma: no cover — the attempt's foreign key
            # is to this row, so it cannot be absent while the attempt is not.
            raise UnknownAttempt(f"no bracket node {attempt.pairing_id}")

        tournament = await self._tournaments.by_id(located.tournament_id)
        if tournament is None or tournament.status is not TournamentStatus.IN_PROGRESS:
            # A cancelled or completed tournament does not move. Not an
            # error: OQ-1 leaves cancellation to the Administration epic,
            # and a match already in flight when one happens will complete.
            logger.info(
                "tournament_completion_ignored",
                extra={"match_id": str(completion.match_id), "reason": "tournament_not_running"},
            )
            return

        advancement = decide(
            attempt,
            outcome=completion.outcome,
            winner_id=self._winner_of(attempt, completion.winner_seat),
            higher_seed_player_id=_higher_seed_of(located.node),
        )

        if advancement.rematch_due:
            await self._rematch(tournament, attempt)
        elif advancement.winner_id is not None and advancement.reason is not None:
            await self._advance(tournament, located, advancement.winner_id, advancement.reason)

        await self._record(attempt, completion)

    async def _rematch(self, tournament: Tournament, attempt: PairingAttempt) -> None:
        """One rematch, sides swapped, same pairing — §6c.

        Swapped rather than repeated: the first attempt's sides came from
        the bracket's alternating rule, and repeating them would give one
        player the first move in both games of a tie.

        Idempotent by `unique (pairing_id, attempt_number)` — the launcher
        returns the stored attempt rather than creating a second match.
        """
        light, dark = rematch_seats(attempt)
        async with self._unit_of_work:
            await self._launcher.launch(
                tournament,
                [
                    PlannedAttempt(
                        pairing_id=attempt.pairing_id,
                        attempt_number=attempt.attempt_number + 1,
                        light_player_id=light,
                        dark_player_id=dark,
                    )
                ],
            )
            await self._unit_of_work.commit()

        logger.info(
            "tournament_rematch_launched",
            extra={"pairing_id": str(attempt.pairing_id), "attempt": attempt.attempt_number + 1},
        )

    async def _advance(
        self,
        tournament: Tournament,
        located: LocatedNode,
        winner_id: UUID,
        reason: AdvancementReason,
    ) -> None:
        """Moves the winner up and asks whether the round is finished.

        `advance_winner` holds the compare-and-set and its own transaction
        (§8); this adds the progression that follows from it, which is a
        separate idempotent step for the same reason materialisation is
        separate from starting.
        """
        await self._brackets.advance_winner(
            tournament.id,
            round_number=located.node.round_number,
            slot=located.node.slot,
            winner_id=winner_id,
            reason=reason,
        )
        await self._progress(tournament, from_round=located.node.round_number)

    async def _progress(self, tournament: Tournament, *, from_round: int) -> None:
        """Completes a finished round and starts whatever follows it — §6b.

        Three outcomes, and the round's own status is what makes each of
        them happen once:

            round unfinished   nothing
            round finished     complete it, publish and launch the next
            final finished     complete the tournament

        Idempotent because every write is guarded on the status it moves
        from: a second call finds the round already `COMPLETED` and stops.
        """
        async with self._unit_of_work:
            nodes = await self._bracket.nodes_for(tournament.id)
            current = [node for node in nodes if node.round_number == from_round]
            if any(node.winner_id is None for node in current):
                await self._unit_of_work.commit()
                return

            await self._complete_round(tournament, from_round)

            following = [node for node in nodes if node.round_number == from_round + 1]
            if not following:
                await self._finish(tournament, current)
            else:
                await self._open_round(tournament, following, round_number=from_round + 1)

            await self._unit_of_work.commit()

    async def _complete_round(self, tournament: Tournament, round_number: int) -> None:
        round_ = await self._round(tournament, round_number)
        if round_ is None or round_.status is RoundStatus.COMPLETED:
            return

        # A round that is still `PUBLISHED` has to pass through
        # `IN_PROGRESS`: the aggregate's table permits no shortcut, and the
        # instant it records is what "when was this round played" reads.
        moved = round_ if round_.status is RoundStatus.IN_PROGRESS else round_.started(self._now())
        await self._rounds.save(moved.completed(self._now()))
        await self._events.publish(
            RoundCompleted(
                occurred_at=self._now(),
                tournament_id=tournament.id,
                round_number=round_number,
            )
        )

    async def _open_round(
        self, tournament: Tournament, nodes: list[BracketSlot], *, round_number: int
    ) -> None:
        """Publishes a round and creates its matches — §6b.

        Publication is what freezes the pairings, and it happens here rather
        than at materialisation because a later round's participants are not
        known until the round beneath it is played (§6b).

        Byes among these nodes were already resolved by the propagation
        `advance_winner` ran, so `plans_for` launches exactly the nodes with
        two participants.
        """
        round_ = await self._round(tournament, round_number)
        if round_ is None or round_.status is not RoundStatus.PENDING:
            # Already open. The launch below still runs, because a worker
            # that died between publishing and launching left matches owed.
            await self._launcher.launch(tournament, plans_for(nodes))
            return

        published = round_.published(self._now())
        await self._rounds.save(published.started(self._now()))
        await self._events.publish(
            RoundPublished(
                occurred_at=self._now(),
                tournament_id=tournament.id,
                round_number=round_number,
            )
        )
        await self._launcher.launch(tournament, plans_for(nodes))

    async def _finish(self, tournament: Tournament, final: list[BracketSlot]) -> None:
        """The final is decided, so the tournament is — §5.

        `COMPLETED` is terminal: a completed tournament is a permanent
        competitive record, and reopening one would make the standings
        somebody read stop being the standings.
        """
        winner_id = next((node.winner_id for node in final if node.winner_id is not None), None)
        if winner_id is None:  # pragma: no cover — the caller checked every
            # node in this round has a winner before reaching here.
            return

        completed = tournament.transitioned_to(TournamentStatus.COMPLETED)
        await self._tournaments.save(completed)
        await self._events.publish(
            TournamentCompleted(
                occurred_at=self._now(), tournament_id=tournament.id, winner_id=winner_id
            )
        )
        logger.info(
            "tournament_completed",
            extra={"tournament_id": str(tournament.id), "winner_id": str(winner_id)},
        )

    async def _record(self, attempt: PairingAttempt, completion: CompletedTournamentMatch) -> None:
        """Marks the attempt completed. **Last**, and not a gate.

        `complete` returning `False` means another delivery already recorded
        this result. It says nothing about whether the advancement that
        result implies was carried out, which is why nothing above depends
        on it — see this module's docstring.
        """
        async with self._unit_of_work:
            await self._attempts.complete(
                replace(
                    attempt,
                    status=AttemptStatus.COMPLETED,
                    outcome=completion.outcome,
                    winner_id=self._winner_of(attempt, completion.winner_seat),
                    completed_at=self._now(),
                )
            )
            await self._unit_of_work.commit()

    async def _round(self, tournament: Tournament, round_number: int) -> TournamentRound | None:
        rounds = await self._rounds.rounds_for(tournament.id)
        return next((r for r in rounds if r.round_number == round_number), None)

    def _winner_of(self, attempt: PairingAttempt, seat: str | None) -> UUID | None:
        """Which player the winning seat names, for **this attempt**.

        Read from the attempt rather than from the pairing, because a
        rematch swaps the seats: `"light"` in attempt two is the player who
        was dark in attempt one.
        """
        if seat == LIGHT_SEAT:
            return attempt.light_player_id
        if seat == DARK_SEAT:
            return attempt.dark_player_id
        return None

    def _now(self) -> datetime:
        return self._clock.now()


def _higher_seed_of(node: BracketSlot) -> UUID:
    """The better-seeded of a node's two participants — §6c's tie-break.

    From the **pairing**, never from an attempt's seats: a rematch swaps
    those, and an adjudication that depended on which game of a tie it was
    would be a coin flip wearing a rule's clothes.

    Seed numbers count up from the best, so the smaller number wins. A node
    that reaches here has two seated players — `ck_pairing__*_seat_is_complete`
    pairs each player with a seed — so a missing one is a malformed row
    rather than a case to default.
    """
    seats = [
        (seed, player)
        for seed, player in (
            (node.light_seed, node.light_player_id),
            (node.dark_seed, node.dark_player_id),
        )
        if seed is not None and player is not None
    ]
    if not seats:
        raise InvalidBracketPosition(
            f"round {node.round_number} slot {node.slot} has no seeded participant"
        )
    return min(seats)[1]


__all__ = [
    "CompletedTournamentMatch",
    "TournamentAdvancementService",
    "UnknownAttempt",
]
