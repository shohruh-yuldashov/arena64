"""`GameCommandService` — resigning and agreeing a draw, atomically.
A64-020.5C-pre §5, §6, §11, §12.

    lock the row  ->  resolve the caller to a side  ->  apply one domain
                  ->  transition  ->  compare-and-set  ->  cancel the
                  ->  deadline  ->  stage the outbox event

Deliberately the same shape as `LiveMoveService.submit`, minus the two
steps a command does not have: no move row and no engine. Everything that
made that flow safe is here for the identical reason.

## Why the aggregate is not rebuilt

`LiveMoveService` replays the durable log into a `Match` on every
submission, because a move needs a position to be legal in and draw rules
that count repetitions. **None of these commands touches a board.** A
resignation is a statement about the players (GE-67), and a draw agreement
is a statement about their intentions; both settle the same way whatever
the pieces are doing.

So the replay is skipped and the rule still comes from the domain:
`resignation_result` and `agreed_draw_result` live beside `Match.resign`
and are what it uses, so there is exactly one definition of "the opponent
wins". Rebuilding an O(plies) aggregate to reach a method that ignores its
own board would be work done to look principled.

## The transaction — §5

One transaction per command, and it spans the match write **and** the
outbox row. That is AD-16 and it is the whole of §5's "do not partially
settle a Match and then emit an event in another transaction": an event
for a resignation that rolled back would tell `rating` a game ended that
nobody lost.

Not committed here. The caller's unit of work is the boundary, exactly as
`LiveMoveService` leaves it, which is what lets a future caller run two
things in one transaction without this service knowing.

## Concurrency — §6

Two mechanisms, and between them they cover every race §6 names:

    the row lock     `FOR UPDATE`, no `SKIP LOCKED`. Serialises the two
                     players of one match against each other and against
                     their own moves — a resignation racing an accepted
                     draw is two transactions queuing on one row, and the
                     second sees what the first wrote

    advance's CAS    `ply_number` and `status = active`. A second terminal
                     command finds the match completed under the lock and
                     is refused before the write; the CAS is what holds if
                     a future path reads without locking

**Exactly one terminal result wins** because the status check happens under
the lock: `MatchRecord._require_active` refuses anything that is no longer
active, and a completed match is no longer active. There is no path by
which a match ends both by resignation and by agreed draw.

A process-local lock is forbidden (§6) and would be wrong anyway: the two
players may be served by different gateway nodes.

## The clock — §11

`deadlines.cancel` on a terminal command, and nothing else. A stale flag
worker already loses on its own: `ClockAdjudicationService._is_current`
checks `status is ACTIVE` against the authoritative row under its lock, so
a match settled by resignation makes every outstanding deadline token
non-current. Cancelling is the optimisation that stops a worker claiming
work it would then correctly refuse — not the correctness mechanism.

An offer and a decline **do not touch the clock at all**. A player who
offers a draw is still on the clock, which is the only defensible rule: the
alternative is a free pause available on demand.
"""

import logging
from datetime import datetime

from app.core.clock import Clock
from app.modules.engine import PlayerSide
from app.modules.game.application.ports import ClockDeadlineStore, MatchRecordRepository
from app.modules.game.domain.events import MatchCompleted, SeatSummary
from app.modules.game.domain.exceptions import MatchNotFound, StaleMatchState
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.public.commands import (
    DrawOfferView,
    GameCommand,
    GameCommandRequest,
    GameCommandResult,
)
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


class GameCommandService:
    """`GameCommandUseCase` over the match row, the deadlines and the outbox."""

    def __init__(
        self,
        *,
        matches: MatchRecordRepository,
        deadlines: ClockDeadlineStore,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._matches = matches
        self._deadlines = deadlines
        self._events = events
        self._clock = clock

    async def execute(self, request: GameCommandRequest) -> GameCommandResult:
        """Runs one command. Does **not** commit — see the module docstring.

        The order below is the safety argument: the lock is taken before
        the side is resolved, so two players acting at once queue rather
        than both reading a stale row; the domain transition runs before
        any write, so a refusal leaves nothing to roll back; and the event
        is staged after the compare-and-set, so an event cannot outlive a
        write that lost.
        """
        at = request.received_at or self._clock.now()

        record = await self._locked(request)
        side = record.side_of(request.player_id)

        settled = _applied(record, request.command, side=side, at=at)
        if not await self._matches.advance(settled, expected_ply=record.ply_number):
            raise StaleMatchState("The match moved on; re-read and retry.")

        if settled.status is not MatchRecordStatus.ACTIVE:
            await self._cancel_deadline(settled)
            await self._publish_completion(settled, at=at)

        logger.info(
            "game_command_applied",
            extra={
                "match_id": str(settled.id),
                "command": request.command.value,
                "side": side.value,
                "ply": settled.ply_number,
                "terminal": settled.outcome is not None,
            },
        )
        return _result_for(request, settled, side=side)

    async def _locked(self, request: GameCommandRequest) -> MatchRecord:
        """The match row, locked for this transaction — §6.

        `FOR UPDATE` and **not** `SKIP LOCKED`, the same choice the move
        path and the acceptance handshake both make: the second actor has
        nowhere else to go and must wait and then see what the first wrote.

        Identity before state, matching `LiveMoveService._locked`: a caller
        who may not see this match is told the match does not exist, so
        live identifiers stay unenumerable. The `active` check is
        deliberately **not** here — it belongs to the domain transitions,
        which each refuse for their own reason with their own message.
        """
        record = await self._matches.lock(request.match_id)
        if record is None or request.player_id not in record.player_ids():
            raise MatchNotFound("No such match.")
        return record

    async def _cancel_deadline(self, record: MatchRecord) -> None:
        """Drops the settled match's clock deadline — §11.

        **Never raises.** The match is already settled and about to be
        committed, so a deadline that could not be removed is one a worker
        will claim and then correctly refuse — wasted work, not a wrong
        result. Failing the command instead would lose a resignation the
        player already made.
        """
        if record.time_control is None:
            return

        try:
            await self._deadlines.cancel(record.id)
        except Exception as exc:  # noqa: BLE001 — a deadline must not fail a command
            logger.error(
                "clock_deadline_cancel_failed",
                extra={"match_id": str(record.id), "error": type(exc).__name__},
                exc_info=exc,
            )

    async def _publish_completion(self, record: MatchRecord, *, at: datetime) -> None:
        """Stages `MatchCompleted` — §12, AD-16.

        In the **same transaction** as the match write, so an event for a
        settlement that rolled back cannot exist.

        The **same event** the move path and the flag worker publish, with
        the same fields, because `rating`, `statistics` and `tournaments`
        already consume it — §12 forbids a new rating path and a second
        completion event, and reusing this one is how both hold. A
        resignation and a checkmate are indistinguishable to a consumer
        except by `termination_reason`, which is exactly right.
        """
        result = record.result
        if result is None:  # pragma: no cover — guarded by the caller's status check
            return

        await self._events.publish(
            MatchCompleted(
                occurred_at=at,
                match_id=record.id,
                variant=record.variant,
                rated=record.rated,
                outcome=result.outcome,
                termination_reason=result.reason,
                winner=result.winner,
                ply_number=record.ply_number,
                engine_version=record.engine_version.as_primitive(),
                speed_class=record.light.rating.speed_class if record.light.rating else None,
                light=_seat_summary(record.light),
                dark=_seat_summary(record.dark),
                # R-25's round trip: the originating context recognises its
                # own match here rather than by reading `game`'s table.
                origin=record.origin,
                origin_ref=record.origin_ref,
            )
        )
        logger.info(
            "match_completed",
            extra={
                "match_id": str(record.id),
                "outcome": result.outcome.value,
                "reason": result.reason.value,
                "plies": record.ply_number,
            },
        )


def _applied(
    record: MatchRecord, command: GameCommand, *, side: PlayerSide, at: datetime
) -> MatchRecord:
    """The record this command produces, or the domain's refusal.

    A mapping rather than a chain of branches, so the four commands and the
    four transitions are one table somebody can read — and so a fifth
    command that forgot its transition is a `KeyError` at the boundary
    rather than a silently ignored frame.
    """
    transitions = {
        GameCommand.RESIGN: record.resigned_by,
        GameCommand.OFFER_DRAW: record.offered_draw,
        GameCommand.ACCEPT_DRAW: record.accepted_draw,
        GameCommand.DECLINE_DRAW: record.declined_draw,
    }
    return transitions[command](side, at=at)


def _result_for(
    request: GameCommandRequest, record: MatchRecord, *, side: PlayerSide
) -> GameCommandResult:
    offer = record.draw_agreement.offer
    return GameCommandResult(
        match_id=record.id,
        command=request.command,
        acting_side=side,
        ply=record.ply_number,
        offer=(
            DrawOfferView(
                offered_by=offer.offered_by,
                offered_at_ply=offer.offered_at_ply,
                offered_at=offer.offered_at,
            )
            if offer is not None
            else None
        ),
        outcome=record.outcome,
        termination_reason=record.termination_reason,
        winner=record.winner,
        settled_at=record.ended_at,
    )


def _seat_summary(seat: MatchSeat) -> SeatSummary | None:
    """A seat's persisted rating snapshot, for the completion event.

    `None` when the seat has none — a match created before A64-017.2.
    `rating` treats that as "not rateable", which is correct: nothing
    captured what these players rated, so nothing can compute what they
    should rate now without inventing it.
    """
    if seat.rating is None:
        return None
    return SeatSummary(
        player_id=seat.player_id,
        rating_value=seat.rating.value,
        rating_deviation=seat.rating.deviation,
        rating_volatility=seat.rating.volatility,
        games_played=seat.rating.games_played,
        is_provisional=seat.rating.is_provisional,
    )


__all__ = ["GameCommandService"]
