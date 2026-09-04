"""`ClockAdjudicationService` — flagging a match nobody moved in.
A64-016.5 §6.

AD-21's worker. It claims expired deadlines from Redis, checks each against
the authoritative match row, and settles the ones that really have run out.

## Why a claimed deadline is a token rather than a verdict

The deadline says "at ply 7, DARK's clock ran out". By the time a worker
reads it, three things may have happened: DARK may have moved (ply is now 8),
the match may have completed some other way, or another worker may have
already settled it. So the claim is checked against the match row before
anything is written, and §6's four conditions are exactly that check:

    match is still active        the row's status
    deadline version matches     the row's ply_number
    active side matches          the clock's active side
    no later move superseded it  implied by the first two — a later move
                                 advances the ply, so a stale token cannot
                                 match

A deadline that fails any of them is **dropped silently**, not retried: it
was superseded, which is the ordinary outcome of a player moving with
milliseconds to spare, and the move that superseded it already wrote a new
deadline.

## Idempotency

Two ways it is safe to run twice, and both are needed:

**The claim removes.** `claim_expired` is one Lua script, so two workers
receive disjoint sets and the same deadline cannot be adjudicated twice
concurrently.

**The write is conditional.** `advance` is a compare-and-set on the ply, so
even if two workers somehow held the same token, one write lands and the
other reports `False` — which is dropped, because a match that is already
completed is the outcome that was wanted.

## What it deliberately does not do

No retry, no backoff, no dead-lettering. A deadline that could not be settled
because the database was unreachable is simply gone, and the match stops
flagging until its next move writes a new one — which for a game nobody is
moving in means it stays open.

That is a real limitation and it is stated rather than hidden: the correct
fix is a sweep that re-derives deadlines from active matches, which is a
recovery job rather than part of adjudication. Recorded in
`docs/01-architecture/websocket.md` §19.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.game.application.ports import (
    ClaimedDeadline,
    ClockDeadlineStore,
    MatchRecordRepository,
)
from app.modules.game.domain.events import MatchCompleted, SeatSummary
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.domain.result import MatchOutcome, MatchResult, TerminationReason
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdjudicationRun:
    """What one pass of the worker did.

    Three counts rather than a total, because they mean different things to
    an operator: `settled` is games that ended, `superseded` is players who
    moved just in time — which is *normal* and should be the large number —
    and `failed` is the one an alert fires on.
    """

    claimed: int
    settled: int
    superseded: int
    failed: int

    @property
    def is_idle(self) -> bool:
        return self.claimed == 0


class ClockAdjudicationService:
    """Settles matches whose clock ran out — AD-21."""

    def __init__(
        self,
        *,
        matches: MatchRecordRepository,
        deadlines: ClockDeadlineStore,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
        batch_size: int,
    ) -> None:
        self._matches = matches
        self._deadlines = deadlines
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._batch_size = batch_size

    async def adjudicate_once(self) -> AdjudicationRun:
        """One bounded pass. Never raises.

        A worker that propagated would stop the schedule that called it —
        the same argument `OutboxPruner.prune_once` and
        `QueueRetentionService.prune_once` both make — and a clock worker
        that has silently stopped is invisible until somebody's game hangs.

        **One transaction per deadline**, not per batch. Two matches
        flagging in the same pass are unrelated, and a batch transaction
        would make one match's failure roll back another's settlement.
        """
        now = self._clock.now()

        try:
            claimed = await self._deadlines.claim_expired(now=now, limit=self._batch_size)
        except Exception as exc:  # noqa: BLE001 — a maintenance job must not escalate
            logger.error(
                "clock_deadline_claim_failed",
                extra={"error": type(exc).__name__},
                exc_info=exc,
            )
            return AdjudicationRun(claimed=0, settled=0, superseded=0, failed=0)

        settled = 0
        superseded = 0
        failed = 0

        for deadline in claimed:
            outcome = await self._settle(deadline, now=now)
            if outcome is _Outcome.SETTLED:
                settled += 1
            elif outcome is _Outcome.SUPERSEDED:
                superseded += 1
            else:
                failed += 1

        if claimed:
            logger.info(
                "clock_adjudication_completed",
                extra={
                    "claimed": len(claimed),
                    "settled": settled,
                    "superseded": superseded,
                    "failed": failed,
                },
            )
        return AdjudicationRun(
            claimed=len(claimed), settled=settled, superseded=superseded, failed=failed
        )

    async def _settle(self, deadline: ClaimedDeadline, *, now: datetime) -> "_Outcome":
        """Settles one claimed deadline, or reports why it did not."""
        try:
            async with self._unit_of_work:
                record = await self._matches.lock(deadline.match_id)
                if record is None or not _is_current(record, deadline, now=now):
                    # Superseded, already settled, or untimed. The move that
                    # superseded it wrote a new deadline, so there is
                    # nothing to reschedule and nothing to report.
                    await self._unit_of_work.commit()
                    return _Outcome.SUPERSEDED

                await self._flag(record, deadline, now=now)
                await self._unit_of_work.commit()
        except Exception as exc:  # noqa: BLE001 — one match must not stop a pass
            logger.error(
                "clock_adjudication_failed",
                extra={"match_id": str(deadline.match_id), "error": type(exc).__name__},
                exc_info=exc,
            )
            return _Outcome.FAILED

        # Outside the transaction: the deadline is already claimed and
        # removed, so this is belt-and-braces against a store that resurrects
        # one, and a failure here costs a claim that finds nothing.
        await self._forget(deadline)
        return _Outcome.SETTLED

    async def _flag(self, record: MatchRecord, deadline: ClaimedDeadline, *, now: datetime) -> None:
        """Writes the timeout result and stages its event.

        The **opponent wins** — `TerminationReason.FLAG`. There is no
        insufficient-material check: `FLAG_INSUFFICIENT_MATERIAL` exists in
        the taxonomy and firing it needs a material threshold that is one of
        the undecided product decisions (§8), so the platform awards the win
        rather than guessing a draw. Stated in the spec rather than left as
        a surprise.
        """
        result = MatchResult(
            outcome=MatchOutcome.WIN,
            reason=TerminationReason.FLAG,
            winner=deadline.side.opponent(),
        )
        settled = record.completed(result, ply_number=record.ply_number, at=now)

        if not await self._matches.advance(settled, expected_ply=record.ply_number):
            # Another writer won between the lock and the write. Under the
            # row lock this is unreachable; it is kept because §6 asks for
            # the guarantee rather than for the lock.
            raise _Superseded(deadline.match_id)

        await self._events.publish(
            MatchCompleted(
                occurred_at=now,
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
                # A match that flags is as much a completion as one that is
                # played out, so the originating context must recognise it
                # here too — see `MatchCompleted`.
                origin=record.origin,
                origin_ref=record.origin_ref,
            )
        )
        logger.info(
            "clock_flagged",
            extra={
                "match_id": str(record.id),
                "ply": record.ply_number,
                "flagged_side": deadline.side.value,
            },
        )

    async def _forget(self, deadline: ClaimedDeadline) -> None:
        try:
            await self._deadlines.cancel(deadline.match_id)
        except Exception as exc:  # noqa: BLE001 — the claim already removed it
            logger.warning("clock_deadline_cancel_failed", extra={"error": type(exc).__name__})


def _is_current(record: MatchRecord, deadline: ClaimedDeadline, *, now: datetime) -> bool:
    """Whether this token still describes the match — §6's four conditions.

    Every one is a comparison against the **authoritative row**, read under
    its lock. A deadline that fails any of them was superseded by a move,
    which is the ordinary outcome of a player moving with milliseconds to
    spare.

    The last check is the one that matters most and is easy to omit: the
    clock must *still* be expired as of now. A deadline can be claimed a
    moment early by a worker whose clock runs fast, and flagging on that
    would take a game from somebody who still had time.
    """
    return (
        record.status is MatchRecordStatus.ACTIVE
        and record.ply_number == deadline.ply_number
        and record.clock is not None
        and record.clock.active_side is deadline.side
        and record.clock.has_flagged(now)
    )


class _Superseded(Exception):
    """A conditional write lost. Caught by `_settle` and reported as a
    failure rather than a settlement — see `_flag`."""


class _Outcome(StrEnum):
    """What one claimed deadline turned into.

    Private to this module and never serialised. A `StrEnum` rather than
    three constants because the three are compared by identity and a typo
    in one comparison would silently count a settlement as a failure.
    """

    SETTLED = "settled"
    SUPERSEDED = "superseded"
    FAILED = "failed"


__all__ = ["AdjudicationRun", "ClockAdjudicationService"]


def seat_summary(seat: MatchSeat) -> SeatSummary | None:
    """A seat's persisted rating snapshot, for the completion event.

    Public since A64-025.13A: `PersistentMatchAdjudication` publishes the
    same event and needs the same mapping, and two copies of a mapper are
    two places for a new rating field to be forgotten.

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
