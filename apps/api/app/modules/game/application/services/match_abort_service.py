"""`PersistentMatchAbort` — closing a fixture nobody played.
A64-025.13A §36.

`game.public.abort` states the defect this closes and why the ending is an
abort rather than a win. This is the implementation, and it is deliberately
the smallest thing that can close it: one lock, one transition, one event.

## Why it mirrors the clock adjudicator rather than the command handler

Both of those end a match, and only one is the right shape. The command
handler starts from a participant and checks that they are in the match; the
clock adjudicator starts from a system deadline and checks the match against
it. This starts from a system verdict, so it is the second — lock, re-read
authority, transition, publish, commit.

`MatchCompleted` carries `origin` and `origin_ref` for the reason the clock
adjudicator records: an aborted match is as much a completion as one played
out, and every consumer has to settle it. Without the event the tournament's
own reconciler keeps re-reading a match it thinks is unfinished, and the
gateway leaves a room open on a game that has ended.

`MatchOutcome.NONE` is what keeps `rating` out of it: MT-11 excludes an
abort from every rating and statistic, so publishing this cannot move a
Glicko-2 number for a game nobody played.
"""

import logging
from datetime import datetime

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.game.application.ports import MatchRecordRepository
from app.modules.game.application.services.clock_adjudication_service import seat_summary
from app.modules.game.domain.events import MatchCompleted
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus
from app.modules.game.domain.result import MatchOutcome, MatchResult, TerminationReason
from app.modules.game.public.abort import AbortMatchRequest, AbortOutcome
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


class PersistentMatchAbort:
    """`game.public.abort.MatchAbortUseCase`, over one session.

    Ports only — a repository, a publisher, a unit of work and a clock — so
    the whole flow is testable with no database and no timer.
    """

    def __init__(
        self,
        *,
        matches: MatchRecordRepository,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._matches = matches
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def abort(self, request: AbortMatchRequest) -> AbortOutcome:
        """Closes the match with no result, or says why it did not.

        The **row lock is taken before the status is read**, which is what
        makes the idempotency real rather than likely: two sweeps holding
        the same attempt serialise here, the first closes it, and the second
        reads a completed row and reports `ALREADY_SETTLED`.
        """
        async with self._unit_of_work:
            record = await self._matches.lock(request.match_id)
            if record is None:
                await self._unit_of_work.commit()
                logger.error(
                    "match_abort_unknown_match",
                    extra={"match_id": str(request.match_id)},
                )
                return AbortOutcome.NOT_FOUND

            # A result that arrived while the caller held its claim wins.
            # The same rule `TournamentNoShowService` applies to a superseded
            # attempt, applied on this side of the boundary too, so that a
            # caller which forgot it cannot close a game that was played.
            if record.status is not MatchRecordStatus.ACTIVE:
                await self._unit_of_work.commit()
                return AbortOutcome.ALREADY_SETTLED

            await self._close(record, at=self._clock.now())
            await self._unit_of_work.commit()

        return AbortOutcome.ABORTED

    async def _close(self, record: MatchRecord, *, at: datetime) -> None:
        """Writes the abort and stages its event.

        `expected_ply=record.ply_number` is the same compare-and-set the
        clock adjudicator uses. Under the row lock it cannot fail; it is here
        because the guarantee belongs to the write rather than to the lock,
        and a future caller reaching this without locking would otherwise be
        silently unsafe.
        """
        result = MatchResult(
            outcome=MatchOutcome.NONE,
            reason=TerminationReason.ABORT,
            winner=None,
        )
        settled = record.completed(result, ply_number=record.ply_number, at=at)

        if not await self._matches.advance(settled, expected_ply=record.ply_number):
            # Unreachable under the lock, and not swallowed: a caller told
            # this succeeded when it did not would advance a bracket past a
            # match that is still open.
            raise RuntimeError(f"match {record.id} moved under an abort write")

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
                light=seat_summary(record.light),
                dark=seat_summary(record.dark),
                origin=record.origin,
                origin_ref=record.origin_ref,
            )
        )
        logger.warning(
            "match_aborted",
            extra={
                "match_id": str(record.id),
                "ply": record.ply_number,
                "origin": record.origin.value,
                "origin_ref": str(record.origin_ref) if record.origin_ref else None,
            },
        )


__all__ = ["PersistentMatchAbort"]
