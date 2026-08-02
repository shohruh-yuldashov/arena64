"""`PairingReconciliationService` — the recovery A64-015.3 left to a human.

A64-015.3 shipped pairing as three transactions with a compensation between
them, and recorded the gap honestly: a worker that died after `game`
committed but before it settled the tickets left "a match [that] exists
whose tickets do not say so", logged at `ERROR` with the words "the
reconciliation is manual". A64-015.4 §9 replaces that log line with this
service.

## The four states it recognises, and the action each implies

Everything this job does is derived from **durable state** rather than from
remembering what a dead worker intended. It claims reservations that have
stood past their own deadline, asks `game` whether each produced a match,
and acts:

    reservation, match exists      settle the ticket as `matched`, with
                                   the match's own `created_at`
    reservation, no match, in date return the ticket to `waiting` with the
                                   `entered_at` it always had
    reservation, no match, overdue expire it — releasing a ticket past its
                                   own `expires_at` would put somebody back
                                   in a queue that had stopped honouring
                                   them
    pending match, window closed   expire the match, through `game`'s own
                                   published sweep

The first is "match created but tickets not settled"; the second and third
are "orphaned reserved tickets" and "reservation with no match"; the fourth
is "expired acceptance deadline". A declined or cancelled match needs no
entry, and that is worth stating rather than leaving as an omission: its
tickets were settled as `matched` the moment the match was created, so
there is nothing stranded to recover. See this module's "what a declined
match does not do" below.

## Why it is idempotent, and how

Every write is a compare-and-set on the status the reconciler read. A
second worker that reaches the same ticket finds it no longer `reserved`
and its update applies to nothing; a redelivered task claims a set that is
already empty. Nothing here counts, accumulates or remembers between ticks,
so running it twice is running it once.

## Why it is two transactions

The claim commits on its own, so the rows this worker took are visibly
locked before anything else happens — exactly `QueueService.expire_due`'s
argument, and `SKIP LOCKED` is what makes a second reconciler skip rather
than wait. The settlements then commit with their events.

The cross-context read sits **between** them, outside any transaction,
because services.md BE-05 forbids a cross-context call inside an open one.
That is the same rule that created the gap this service exists to close,
and obeying it here rather than reaching into `game.match` directly is what
keeps the two schemas separable.

## What a declined match does *not* do

Nothing. A64-015.4 §10 asks for an explicit acceptance-failure policy and
this is it, stated where the code that would implement an alternative would
go: when a player declines or lets the window close, **both queue tickets
stay `matched` and neither player is re-queued**. They must join the queue
again.

That is the safest minimal behaviour, and it is chosen rather than found —
`specs/matchmaking.md` lists "what a declined acceptance does to both
tickets" as an *open* specification item, so there is no product policy to
follow. Re-queueing the accepting player automatically would mean this
service minting a queue ticket on somebody's behalf, which drags in QT-1's
uniqueness, the eligibility policy, a fresh rating snapshot and a decision
about whose `entered_at` survives — four product questions, none of them
answered, on a path that runs unattended. The failure mode of getting any
of them wrong is a player holding a ticket they did not ask for, or two.

The cost is real and is not hidden: a player who accepts promptly and whose
opponent declines loses their place in line through no fault of their own.
That is recorded in `specs/matchmaking.md` as the open question it is.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.game.public import (
    MatchAcceptanceExpiryUseCase,
    PairingReconciliationReader,
    PairingSettlement,
)
from app.modules.matchmaking.application.ports import QueueRepository
from app.modules.matchmaking.domain.events import PairingReconciled, ReconciliationAction
from app.modules.matchmaking.domain.queue_ticket import QueueTicket
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """What one `reconcile_once` did.

    Returned rather than only logged — the shape `ExpirySweep`,
    `PairingOutcome` and `RelayTick` already use: a test asserts on the
    outcome and the worker logs it once.

    The four counters are separated because an operator acts differently on
    each. A steady trickle of `settled` means workers are dying between two
    transactions; a trickle of `released` means they are dying before
    reaching `game`; `expired_matches` is ordinary product behaviour and
    should dominate.
    """

    claimed: int
    """Stranded reservations this tick took."""

    settled: int
    """Of those, the ones whose match existed and whose ticket caught up."""

    released: int
    """Returned to `waiting` with their original place in line."""

    expired: int
    """Reservations whose ticket had itself fallen due while reserved."""

    expired_matches: int
    """Pending matches nobody answered in time."""

    @property
    def is_idle(self) -> bool:
        return self.claimed == 0 and self.expired_matches == 0


class PairingReconciliationService:
    """The recovery use case. One bounded batch per call.

    Holds ports only — a queue repository, `game`'s two published reads, a
    publisher, a unit of work and a clock — so the whole flow is testable
    with no database, no `game` and no timer.
    """

    def __init__(
        self,
        *,
        tickets: QueueRepository,
        settlements: PairingReconciliationReader,
        acceptance: MatchAcceptanceExpiryUseCase,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
        batch_size: int,
    ) -> None:
        self._tickets = tickets
        self._settlements = settlements
        self._acceptance = acceptance
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._batch_size = batch_size

    async def reconcile_once(self) -> ReconciliationOutcome:
        """Expires overdue pending matches, then recovers stranded
        reservations.

        Never raises. This runs from a scheduled task, and a tick that
        propagated would stop the schedule — the argument
        `QueueService.expire_due`, `PairingService.pair_once` and
        `OutboxRelay.run_once` all make. Every failure is recorded and
        returned as an outcome instead.

        **Matches first, tickets second**, and the order matters: expiring
        a match settles nothing about its tickets (they are already
        `matched`), but doing it first means a reservation whose match has
        just been abandoned is seen in its final state rather than in a
        state that is about to change.
        """
        expired_matches = await self._expire_overdue_matches()
        claimed = await self._claim()
        if not claimed:
            return ReconciliationOutcome(
                claimed=0,
                settled=0,
                released=0,
                expired=0,
                expired_matches=expired_matches,
            )

        try:
            settlements = await self._settlements.settlements_for([ticket.id for ticket in claimed])
        except Exception as error:  # noqa: BLE001 — a background tick must not escalate
            # There is no safe default here: guessing "no match" releases a
            # ticket whose player already has a game, and guessing
            # "matched" strands one who does not. The reservations are
            # untouched and still stale, so the next tick claims them
            # again.
            logger.error(
                "pairing_reconciliation_read_failed",
                extra={"claimed": len(claimed), "error": type(error).__name__},
                exc_info=error,
            )
            return ReconciliationOutcome(
                claimed=len(claimed),
                settled=0,
                released=0,
                expired=0,
                expired_matches=expired_matches,
            )

        return await self._apply(claimed, settlements, expired_matches=expired_matches)

    async def _expire_overdue_matches(self) -> int:
        """`game`'s half — pending matches whose window closed.

        Reached through `MatchAcceptanceExpiryUseCase` rather than by
        touching `game.match`, so the only thing this module knows about a
        match's lifecycle is that somebody else owns it.
        """
        try:
            return len(await self._acceptance.expire_overdue(limit=self._batch_size))
        except Exception as error:  # noqa: BLE001 — one half failing must not stop the other
            logger.error(
                "match_acceptance_expiry_unavailable",
                extra={"error": type(error).__name__},
                exc_info=error,
            )
            return 0

    async def _claim(self) -> Sequence[QueueTicket]:
        """The claim's own transaction, committed immediately so the lock is
        visible to every other reconciler before any decision is made."""
        try:
            async with self._unit_of_work:
                claimed = await self._tickets.claim_stale_reservations(
                    now=self._clock.now(), limit=self._batch_size
                )
                await self._unit_of_work.commit()
        except Exception as error:  # noqa: BLE001 — a background tick must not escalate
            logger.error(
                "pairing_reconciliation_claim_failed",
                extra={"error": type(error).__name__},
                exc_info=error,
            )
            return ()
        return claimed

    async def _apply(
        self,
        claimed: Sequence[QueueTicket],
        settlements: Mapping[UUID, PairingSettlement],
        *,
        expired_matches: int,
    ) -> ReconciliationOutcome:
        """One transaction: every transition this tick decided, and the
        event for each.

        The settlements are written **one ticket at a time** rather than as
        a batch, deliberately. `QueueRepository.complete` records the
        instant the match was created, and two stranded reservations
        recovered in one tick may belong to two different matches — a
        batched write would have to pick one instant and stamp both with
        it, which is exactly the "when did this player's game start"
        question a settled ticket exists to answer.

        The releases and the expiries *are* batched, because neither
        carries a per-ticket instant: a release writes no instant at all,
        and an expiry writes this tick's.

        The whole batch is bounded by `MATCHMAKING_RECONCILIATION_BATCH_SIZE`
        (CLAUDE.md §10.5), and this path runs only after something already
        went wrong, so a handful of statements per tick is the right trade
        for correct instants.
        """
        now = self._clock.now()
        settled: list[tuple[QueueTicket, PairingSettlement]] = []
        released: list[QueueTicket] = []
        expired: list[QueueTicket] = []

        for ticket in claimed:
            settlement = settlements.get(ticket.id)
            if settlement is not None:
                settled.append((ticket, settlement))
            elif ticket.is_due(now):
                expired.append(ticket)
            else:
                released.append(ticket)

        try:
            async with self._unit_of_work:
                settled_count = await self._settle(settled)
                released_count = await self._release(released)
                expired_count = await self._expire(expired, at=now)
                await self._unit_of_work.commit()
        except Exception as error:  # noqa: BLE001 — a background tick must not escalate
            # Nothing is lost: the reservations are still `reserved` and
            # still stale, so the next tick claims them again.
            logger.error(
                "pairing_reconciliation_write_failed",
                extra={"claimed": len(claimed), "error": type(error).__name__},
                exc_info=error,
            )
            return ReconciliationOutcome(
                claimed=len(claimed),
                settled=0,
                released=0,
                expired=0,
                expired_matches=expired_matches,
            )

        # One line for the batch rather than one per ticket — a deploy that
        # strands two hundred reservations would otherwise bury whatever
        # else was happening (CLAUDE.md §8.8).
        logger.info(
            "pairing_reconciled",
            extra={
                "claimed": len(claimed),
                "settled": settled_count,
                "released": released_count,
                "expired": expired_count,
                "expired_matches": expired_matches,
            },
        )
        return ReconciliationOutcome(
            claimed=len(claimed),
            settled=settled_count,
            released=released_count,
            expired=expired_count,
            expired_matches=expired_matches,
        )

    async def _settle(self, settled: Sequence[tuple[QueueTicket, PairingSettlement]]) -> int:
        """Marks each ticket `matched` with its own match's instant."""
        applied = 0
        for ticket, settlement in settled:
            if not await self._tickets.complete(
                [ticket.matched(settlement.created_at)], at=settlement.created_at
            ):
                # Somebody else reconciled it between the claim and this
                # write. Not a failure and not an event — the outcome the
                # other worker recorded is the same one.
                continue
            applied += 1
            await self._publish(
                ticket, action=ReconciliationAction.SETTLED, match_id=settlement.match_id
            )
        return applied

    async def _release(self, released: Sequence[QueueTicket]) -> int:
        """Returns reservations with no match to `waiting`, in one
        statement.

        All-or-nothing, like every batched transition on this repository.
        A partial batch means another worker moved one of these, and the
        whole group is simply reconsidered next tick — by which time the
        moved ticket is no longer `reserved` and no longer claimed.
        """
        if not released:
            return 0
        if not await self._tickets.release([ticket.released() for ticket in released]):
            logger.debug(
                "pairing_reconciliation_release_contended", extra={"tickets": len(released)}
            )
            return 0
        for ticket in released:
            await self._publish(ticket, action=ReconciliationAction.RELEASED, match_id=None)
        return len(released)

    async def _expire(self, expired: Sequence[QueueTicket], *, at: datetime) -> int:
        """Expires reservations whose ticket had itself fallen due.

        `QueueRepository.expire` rather than `release`, because putting a
        ticket back into `waiting` past its own `expires_at` would return
        somebody to a queue that had already stopped considering them —
        and `active_ticket` would then report them as not queued while
        QT-1's index still held their slot.
        """
        if not expired:
            return 0
        applied = await self._tickets.expire([ticket.id for ticket in expired], at=at)
        for ticket in expired:
            await self._publish(ticket, action=ReconciliationAction.EXPIRED, match_id=None)
        return applied

    async def _publish(
        self, ticket: QueueTicket, *, action: ReconciliationAction, match_id: UUID | None
    ) -> None:
        """One `PairingReconciled`, inside the caller's transaction.

        `occurred_at` is the deadline the reservation overran rather than
        this tick's instant, for the reason `QueueTicketExpired` uses its
        ticket's `expires_at`: the fact became true when the window closed,
        and how late the reconciler was is the job's property rather than
        the pairing's.
        """
        if ticket.reserved_until is None:  # pragma: no cover — reserved implies a deadline
            return
        await self._events.publish(
            PairingReconciled(
                occurred_at=ticket.reserved_until,
                ticket_id=ticket.id,
                player_id=ticket.player_id,
                action=action,
                match_id=match_id,
                reserved_until=ticket.reserved_until,
            )
        )


__all__ = ["PairingReconciliationService", "ReconciliationOutcome"]
