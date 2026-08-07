"""`ChallengeExpiryService` — writing down the expiries the platform has
been assuming since A64-022.1. A64-022.6 §2, §3, §4, §5.

## What was actually missing

`expires_at` has governed *behaviour* since the beginning: the list reads
exclude an overdue row and `_require_answerable` refuses to act on one, so
no challenge has ever been answerable past its window. What did not exist
was the **record** — `ChallengeStatus.EXPIRED` was a member no row ever
held, and `FriendChallengeExpired` was an event nothing published.

That is the gap this closes, and it is worth being precise about its size:
this is not a rule being enforced for the first time. It is the platform
writing down a transition it had already decided, so that a challenge's row
says what happened to it and a consumer can react to the moment it did.

## One transaction, not two — and why this differs from the queue

`QueueService.expire_due` claims in its own transaction, commits, then
resolves in a second. This does both in one, and the difference is the race
each job actually has.

A queue ticket has no competing writer: nothing but the sweeper and a
cancel touches one, and a cancel that loses is harmless because
`expire`'s predicate excludes a resolved ticket. A challenge has a
**competing writer that creates a match** — acceptance — and §5 forbids
"EXPIRED + Match" absolutely.

Holding the `FOR UPDATE` locks from the claim through the commit makes that
outcome unreachable rather than merely unlikely: an acceptance arriving
mid-sweep blocks on the row lock, and when it is released the guarded
`UPDATE` in `ChallengeRepository.save` matches no `PENDING` row and the
recipient is told the challenge was already answered. The reverse ordering
is equally decided — the sweep's own guarded `UPDATE` matches nothing, and
`expire` returns the id set without it, so no expiry event is published for
a challenge that was accepted.

Exactly one of the two wins, and which one is the database's answer rather
than a clock's. `SKIP LOCKED` still lets a second sweeper work in parallel
on a disjoint set, which is the property the two-transaction split existed
to preserve.

## Idempotency needs nothing new — §4

A second sweep over the same rows finds them `EXPIRED`, so `claim_expired`
does not return them: its predicate is `status = 'pending'`. Nothing
transitions twice, `responded_at` is written once, and no second event is
built — because events are built from `expire`'s **returned ids**, and a
row that did not move is not in that set.

An outbox *redelivery* is the relay's own concern and is already handled by
`platform.processed_event`; what this guarantees is that no second *logical*
expiry is ever produced.

## It never raises

This runs from a scheduled task, and a sweep that propagated an exception
would stop the schedule — the argument `OutboxRelay.run_once`,
`PresenceSweeper.sweep_once` and `QueueService.expire_due` all make. A
failure is counted, logged with ids only, and the next tick claims the same
rows again.
"""

import logging
from dataclasses import dataclass
from typing import Final

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.matchmaking.application.metrics import (
    CHALLENGE_EXPIRIES,
    ChallengeExpiryOutcome,
)
from app.modules.matchmaking.application.ports import ChallengeRepository
from app.modules.matchmaking.domain.challenge import Challenge
from app.modules.matchmaking.domain.challenge_events import FriendChallengeExpired
from app.platform.metrics import MetricsRecorder
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)

#: How many claim-and-settle rounds one pass may perform.
#:
#: The batch bounds one transaction; this bounds one *tick*, so a backlog
#: built up while the sweeper was off drains over a few ticks rather than in
#: one transaction holding thousands of row locks. Eight is arbitrary in the
#: way a backstop is: at the default batch of two hundred it is sixteen
#: hundred challenges a minute, which no plausible backlog exceeds.
MAX_ROUNDS: Final = 8


@dataclass(frozen=True, slots=True)
class ChallengeExpirySweep:
    """What one pass did. Returned for the log line and for tests."""

    claimed: int
    expired: int
    """How many rows actually moved. Below `claimed` when an acceptance,
    decline or cancel won the race for one of them — which is a correct
    outcome, not a failure."""

    failed: int = 0


class ChallengeExpiryService:
    """Expires overdue friend challenges, in bounded batches.

    Holds four collaborators and nothing that could reach a match: this
    transition creates no game, so a service able to name `MatchCreationUseCase`
    would be one that could.
    """

    def __init__(
        self,
        *,
        challenges: ChallengeRepository,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
        metrics: MetricsRecorder,
        batch_size: int,
    ) -> None:
        self._challenges = challenges
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._metrics = metrics
        self._batch_size = batch_size

    async def expire_due(self) -> ChallengeExpirySweep:
        """One pass. Never raises.

        Loops until a round claims nothing, bounded by `MAX_ROUNDS`, so a
        backlog drains without one transaction growing with it.
        """
        totals = ChallengeExpirySweep(claimed=0, expired=0)

        for _ in range(MAX_ROUNDS):
            round_result = await self._round()
            totals = ChallengeExpirySweep(
                claimed=totals.claimed + round_result.claimed,
                expired=totals.expired + round_result.expired,
                failed=totals.failed + round_result.failed,
            )
            # A short round means the backlog is drained. A failed one also
            # stops the pass: retrying the same rows in a tight loop would
            # turn one broken challenge into a busy wait.
            if round_result.claimed < self._batch_size or round_result.failed:
                break

        if totals.claimed:
            logger.info(
                "friend_challenges_expired",
                extra={
                    # Counts only. No challenge id, no player id, no
                    # settings — §17. What an operator needs at 3am is
                    # whether the sweep is keeping up, and that is a number.
                    "claimed": totals.claimed,
                    "expired": totals.expired,
                    "lost_race": totals.claimed - totals.expired,
                    "failed": totals.failed,
                },
            )
        return totals

    async def _round(self) -> ChallengeExpirySweep:
        """One claim-and-settle transaction."""
        now = self._clock.now()
        try:
            async with self._unit_of_work:
                claimed = await self._challenges.claim_expired(now=now, limit=self._batch_size)
                if not claimed:
                    return ChallengeExpirySweep(claimed=0, expired=0)

                moved = await self._challenges.expire(
                    [challenge.id for challenge in claimed], at=now
                )
                for challenge in claimed:
                    if challenge.id in moved:
                        await self._events.publish(_expired(challenge))
                await self._unit_of_work.commit()
        except Exception as error:  # noqa: BLE001 — a sweep must not stop the schedule
            logger.warning(
                "friend_challenge_expiry_failed",
                extra={"error": type(error).__name__},
                exc_info=error,
            )
            self._metrics.increment(
                CHALLENGE_EXPIRIES, labels={"outcome": ChallengeExpiryOutcome.FAILED}
            )
            return ChallengeExpirySweep(claimed=0, expired=0, failed=1)

        self._count(claimed=len(claimed), expired=len(moved))
        return ChallengeExpirySweep(claimed=len(claimed), expired=len(moved))

    def _count(self, *, claimed: int, expired: int) -> None:
        """Two bounded labels, no identifiers — §18."""
        for _ in range(expired):
            self._metrics.increment(
                CHALLENGE_EXPIRIES, labels={"outcome": ChallengeExpiryOutcome.EXPIRED}
            )
        for _ in range(claimed - expired):
            self._metrics.increment(
                CHALLENGE_EXPIRIES, labels={"outcome": ChallengeExpiryOutcome.LOST_RACE}
            )


def _expired(challenge: Challenge) -> FriendChallengeExpired:
    """The event for one challenge that actually moved.

    `occurred_at` is the challenge's **own deadline**, not the sweep's
    instant — the same choice `QueueTicketExpired` makes and for the same
    reason: the window closed when it closed, and a relay catching up after
    an outage must not report a day-old expiry as having just happened.
    """
    return FriendChallengeExpired(
        occurred_at=challenge.expires_at,
        challenge_id=challenge.id,
        challenger_id=challenge.challenger_id,
        recipient_id=challenge.recipient_id,
    )


__all__ = ["MAX_ROUNDS", "ChallengeExpiryService", "ChallengeExpirySweep"]
