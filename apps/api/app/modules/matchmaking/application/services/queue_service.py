"""`QueueService` — enter a pool, leave one, read your own ticket, and
expire the ones nobody is waiting on any more.

Orchestrates; does not compute (services.md §3.2). The state machine is
`QueueTicket`'s, uniqueness is the partial unique index's, the atomic claim
is the repository's, and what is left here is the four use cases and the
transaction boundary around each.

**No pairing.** A64-014.1 excludes match creation, rating expansion,
acceptance and realtime updates, and nothing in this class approaches any of
them: there is no method that reads two tickets, and `queue_snapshot` is a
read that returns a value rather than a scan that decides anything.

## Why eligibility is a port and not an `if` — A64-015.2

A64-015.1 asked one question at entry, inline: is this player positively
recorded as offline. It was the only question any module could answer.

There will be more — a suspended account, a live sanction, an unfinished
match — and each comes from a different module, most of which do not exist.
A service that grew one `if` per module would end up holding five ports and
answering a question none of them is about. So it holds
`QueueEligibilityPolicy`, which is one port with one method, and the
presence rule moved behind it unchanged. See
`application/eligibility.py` for the rule and for why the refusal names no
cause.

## Where the transactions are

    join           one — the ticket and its event
    leave          one — the resolution and its event
    requeue        one — the replacement ticket and its event
    active_ticket  none — a read
    expire_due     two — the claim, then the resolutions (see `expire_due`)

Every write publishes inside its own unit of work, which is AD-16 exactly:
the event is as durable as the ticket, and a rollback takes both.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.matchmaking.application.eligibility import QueueEligibilityPolicy
from app.modules.matchmaking.application.ports import QueueRepository, RatingSnapshotProvider
from app.modules.matchmaking.domain.events import (
    QueueTicketCancelled,
    QueueTicketEnqueued,
    QueueTicketExpired,
)
from app.modules.matchmaking.domain.exceptions import (
    AlreadyQueued,
    QueueNotPermitted,
    TicketNotWaiting,
)
from app.modules.matchmaking.domain.queue_pool import QueuePool
from app.modules.matchmaking.domain.queue_ticket import QueueSnapshot, QueueTicket
from app.modules.reference.public import TimeControlCatalogue
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExpirySweep:
    """What one `expire_due` did. Returned rather than only logged, so a
    test asserts on the outcome and the worker logs it once — the shape
    `RelayTick` and `SweepResult` already use."""

    claimed: int
    expired: int
    """Of those claimed, the ones this sweep actually resolved. Fewer when
    somebody cancelled a ticket between the claim and the write."""

    @property
    def is_idle(self) -> bool:
        return self.claimed == 0


class QueueService:
    """The four queue use cases.

    Holds ports only — a repository, a rating provider, an eligibility
    policy, a publisher, a unit of work and a clock — so every rule below is
    testable with no database, no Redis and no timer.

    Notably **not** a presence port of any kind any more: presence moved
    behind `QueueEligibilityPolicy`, so this class cannot read it, cannot
    write it, and cannot grow a second opinion about who is online.
    """

    def __init__(
        self,
        *,
        tickets: QueueRepository,
        ratings: RatingSnapshotProvider,
        time_controls: TimeControlCatalogue,
        eligibility: QueueEligibilityPolicy,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
        ticket_ttl_seconds: float,
        snapshot_limit: int,
    ) -> None:
        self._tickets = tickets
        self._ratings = ratings
        self._time_controls = time_controls
        self._eligibility = eligibility
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._ticket_ttl_seconds = ticket_ttl_seconds
        self._snapshot_limit = snapshot_limit

    async def join(self, *, player_id: UUID, pool: QueuePool) -> QueueTicket:
        """Enters `player_id` into a pool and returns their ticket.

        Raises `AlreadyQueued` (409) when they already hold a live one —
        **in any pool**, per QT-1 — `QueueNotPermitted` (422) when the
        eligibility policy refuses, and `UnsupportedTimeControl` (422) when
        the chosen clock is not one the platform offers.

        The pool is validated by `QueuePool` itself: a variant that is not
        offered cannot be constructed into one, so an unsupported pool never
        reaches this method.

        The duplicate check runs before any write so the common rejection
        costs one indexed read, and the partial unique index is what
        actually enforces it under concurrency (BE-06). Both paths raise the
        same type, so a caller cannot tell which caught it — and must not,
        because the answer changes with timing rather than with anything the
        caller did.

        The **time control is resolved here**, not at the route —
        A64-020.5A-pre §8. The pool the caller hands over carries an
        identifier; the catalogue is what turns it into a control, and a
        `UnsupportedTimeControl` (422) is raised for one that is unknown or
        retired. Resolving it in the service rather than the boundary means
        every entry point gets the same refusal for free, and there is only
        one place a ticket's snapshot can come from.

        It is resolved **before** the eligibility check, so a player asking
        for a control that does not exist is told that rather than being
        refused for a cooldown they would also have to fix. A malformed
        request is a `ValidationError` and outranks a rule (CLAUDE.md §9.1).

        The rating and the catalogue are both read *before* the transaction
        opens. Both are cross-context reads, and services.md BE-05 forbids
        one inside an open transaction: the lock-acquisition order becomes
        something nobody can reason about, and a partial failure would leave
        one side committed with no record that reconciliation is owed.
        """
        control = (await self._time_controls.require(pool.time_control_id)).snapshot

        await self._eligibility.require_eligible(player_id, pool=pool)

        if await self._tickets.active_ticket(player_id, now=self._clock.now()) is not None:
            raise AlreadyQueued("You are already in a matchmaking queue.")

        # The ticket records the **value** only: QT-2's rule is that pairing
        # sorts on one deterministic number. The deviation and volatility from
        # the same read reach the seat snapshot at match creation instead
        # (SPEC-RATING §7.6).
        #
        # Keyed by the pool's variant and the *chosen control's* speed class
        # — A64-020.5A-pre §15. A player's blitz rating is what decides who
        # they meet in a blitz pool, and reading their classical one would
        # pair them by a skill they are not about to demonstrate.
        rating = round(
            (
                await self._ratings.rating_for(
                    player_id, variant=pool.variant, speed_class=control.speed_class
                )
            ).value
        )
        at = self._clock.now()
        ticket = QueueTicket.enter(
            player_id=player_id,
            pool=pool,
            time_control=control,
            rating_snapshot=rating,
            at=at,
            ttl=self._ticket_ttl_seconds,
        )

        async with self._unit_of_work:
            stored = await self._tickets.enqueue(ticket)
            await self._events.publish(
                QueueTicketEnqueued(
                    occurred_at=stored.entered_at,
                    ticket_id=stored.id,
                    player_id=stored.player_id,
                    variant=stored.pool.variant,
                    queue_type=stored.queue_type,
                    region=stored.region,
                    rating_snapshot=stored.rating_snapshot,
                    expires_at=stored.expires_at,
                )
            )
            await self._unit_of_work.commit()

        # A64-014.1's "queue joined". Ids, pool and the recorded rating —
        # no username and no display name (services.md §8.5). The rating is
        # here because it is the one input to pairing that a later "why was
        # I matched with them" question cannot be answered without.
        logger.info(
            "queue_joined",
            extra={
                "ticket_id": str(stored.id),
                "player_id": str(stored.player_id),
                "pool": stored.pool.identifier(),
                "rating_snapshot": stored.rating_snapshot,
            },
        )
        return stored

    async def leave(self, *, player_id: UUID) -> bool:
        """Withdraws the player's live ticket. Returns whether there was one.

        **Idempotent**, and never raises for "you were not queued". Two
        reasons, the second of which is the one that matters:

          - `DELETE` is idempotent by HTTP semantics, so a client retrying
            after a dropped response must not be told the resource is gone
            when its own first attempt removed it.
          - A `404` for "not queued" beside a `204` for "was queued" is a
            state oracle on a write. It is a mild one here — a player can
            already read their own ticket — but the platform answers this
            question the same way for friend removal and unblocking, and
            three endpoints with one convention is worth more than a
            marginally more informative status.

        The compare-and-set in `cancel` is what makes the concurrent case
        correct: a ticket the expiry sweep resolved a millisecond earlier
        returns `False` here and is reported as "you were not queued",
        which is true.
        """
        at = self._clock.now()
        ticket = await self._tickets.active_ticket(player_id, now=at)
        if ticket is None:
            logger.debug("queue_leave_noop", extra={"player_id": str(player_id)})
            return False

        cancelled = ticket.cancelled(at)

        async with self._unit_of_work:
            applied = await self._tickets.cancel(cancelled)
            if not applied:
                # Somebody resolved it between the read and the write. No
                # event, because nothing changed — publishing one here is
                # how a cancelled-and-expired ticket ends up announced
                # twice, under two different verbs.
                await self._unit_of_work.rollback()
                logger.debug("queue_leave_lost_race", extra={"player_id": str(player_id)})
                return False

            await self._events.publish(
                QueueTicketCancelled(
                    occurred_at=at,
                    ticket_id=cancelled.id,
                    player_id=cancelled.player_id,
                    variant=cancelled.pool.variant,
                    queue_type=cancelled.queue_type,
                    region=cancelled.region,
                    waited_for_seconds=_waited(cancelled, at),
                )
            )
            await self._unit_of_work.commit()

        logger.info(
            "queue_cancelled",
            extra={
                "ticket_id": str(cancelled.id),
                "player_id": str(cancelled.player_id),
                "pool": cancelled.pool.identifier(),
                "waited_for_seconds": _waited(cancelled, at),
            },
        )
        return True

    async def requeue(self, *, ticket_id: UUID) -> QueueTicket | None:
        """Puts a player back in the queue with the place in line they had
        — A64-015.5 §1 and §2.

        The reusable operation §2 asks for. Its one caller today is
        `MatchOutcomeService`, reacting to a match that failed through no
        fault of this player; a future rematch decline or an aborted game
        would be a second caller and needs nothing new here.

        Returns the new ticket, or `None` when the requeue **correctly did
        not apply**. `None` is not a failure and every branch that produces
        it is an ordinary outcome:

            the source ticket is gone          nothing to restore
            the source never produced a match  `QueueTicket.requeued` refuses
            they already hold a live ticket    QT-1, and they are queued
            they are no longer eligible        signed out, or in cooldown
            somebody already requeued this     the unique index refused it

        ## Idempotency is the index, not a check

        §2 requires the operation to be idempotent, and the enforcement is
        `uq_queue_ticket__requeued_from` — a partial unique index on
        `source_ticket_id`. Two deliveries of one `match_declined` event
        both pass the `active_ticket` read and both insert; only one row
        survives, and the loser is reported as "already done" rather than
        as an error. A check-then-insert would be correct until the relay
        redelivered under load, which is exactly when it does.

        ## Eligibility is re-asked, deliberately

        §2: "blocked, sanctioned, or otherwise ineligible players are not
        blindly requeued". The player accepted a match perhaps thirty
        seconds ago, and in that window they may have signed out, or —
        the case that matters — *declined a different match and earned a
        cooldown*. Requeueing them anyway would let a decline be laundered
        through somebody else's decline.

        The eligibility read happens **before** the transaction opens, for
        the reason `join` reads the rating outside one: it is a
        cross-context call, and services.md BE-05 forbids those inside an
        open transaction.
        """
        source = await self._tickets.by_id(ticket_id)
        if source is None:
            logger.warning("queue_requeue_source_missing", extra={"ticket_id": str(ticket_id)})
            return None

        at = self._clock.now()
        if await self._tickets.active_ticket(source.player_id, now=at) is not None:
            # They are already queued — by a manual re-entry, or by a
            # delivery of this same event that got here first. Either way
            # the outcome §1 wants is already true.
            logger.debug("queue_requeue_already_queued", extra={"ticket_id": str(ticket_id)})
            return None

        try:
            await self._eligibility.require_eligible(source.player_id, pool=source.pool)
        except QueueNotPermitted:
            logger.info(
                "queue_requeue_refused",
                extra={"ticket_id": str(ticket_id), "pool": source.pool.identifier()},
            )
            return None

        try:
            replacement = source.requeued(at=at, ttl=self._ticket_ttl_seconds)
        except TicketNotWaiting:
            # The source never produced a match, so there is nothing this
            # player lost to somebody else's answer. A caller reaching here
            # is a bug in the caller, and it is logged rather than raised
            # because the caller is a background consumer that must not stop.
            logger.error(
                "queue_requeue_source_not_matched",
                extra={"ticket_id": str(ticket_id), "status": source.status.value},
            )
            return None

        async with self._unit_of_work:
            try:
                stored = await self._tickets.enqueue(replacement)
            except AlreadyQueued:
                # QT-1 or the requeue index refused it. Both mean somebody
                # else got there first, which is the outcome we wanted.
                await self._unit_of_work.rollback()
                logger.debug("queue_requeue_lost_race", extra={"ticket_id": str(ticket_id)})
                return None

            await self._events.publish(
                QueueTicketEnqueued(
                    # The **replacement's** `entered_at`, which is the
                    # original's — so a consumer plotting queue entries sees
                    # this player where they actually belong in the
                    # ordering rather than at the moment of recovery.
                    occurred_at=stored.entered_at,
                    ticket_id=stored.id,
                    player_id=stored.player_id,
                    variant=stored.pool.variant,
                    queue_type=stored.queue_type,
                    region=stored.region,
                    rating_snapshot=stored.rating_snapshot,
                    expires_at=stored.expires_at,
                )
            )
            await self._unit_of_work.commit()

        logger.info(
            "queue_requeued",
            extra={
                "ticket_id": str(stored.id),
                "source_ticket_id": str(ticket_id),
                "player_id": str(stored.player_id),
                "pool": stored.pool.identifier(),
                # How much priority the policy actually preserved. The
                # number that says whether §1's fairness rule is doing
                # anything, without naming what happened to them.
                "preserved_wait_seconds": (at - stored.entered_at).total_seconds(),
            },
        )
        return stored

    async def active_ticket(self, *, player_id: UUID) -> QueueTicket | None:
        """The player's live ticket, or `None`.

        Read-only; opens no transaction. Scoped to the caller by
        construction — there is no parameter that could name another
        player's ticket, which is why this needs no ownership check.

        A ticket past its `expires_at` reads as `None` even before the
        sweeper reaches it. See `QueueRepository.active_ticket`: the
        deadline is the rule, and a worker being a few seconds behind must
        not be something a player can observe.
        """
        return await self._tickets.active_ticket(player_id, now=self._clock.now())

    async def snapshot(self, *, pool: QueuePool) -> QueueSnapshot:
        """One pool as it stands — its depth and its oldest live tickets.

        Bounded by `MATCHMAKING_SNAPSHOT_LIMIT`. Used today to tell a
        waiting player how many others are in their pool, and declared in
        the shape A64-014.2's pairing scan needs so that the scan is a new
        caller rather than a new query.
        """
        return await self._tickets.queue_snapshot(
            pool=pool,
            now=self._clock.now(),
            limit=self._snapshot_limit,
        )

    async def expire_due(self, *, limit: int, claimed_by: str) -> ExpirySweep:
        """Expires one bounded batch of tickets whose window has closed.

        Never raises: this runs from a scheduled task, and a sweep that
        propagated an exception would stop the schedule — the same argument
        `OutboxRelay.run_once` and `PresenceSweeper.sweep_once` both make.

        ## Two transactions, deliberately

        The claim commits on its own, so the rows this worker took are
        visibly locked before anything else happens; a second sweeper
        polling mid-batch skips them, which it can only do if the claim's
        transaction is still open — and `SKIP LOCKED` is what makes that a
        skip rather than a wait. The resolutions and their events then
        commit together, so an event exists exactly when the transition it
        announces does.

        The cost is that a worker dying between the two leaves tickets
        claimed-but-unresolved. That is the correct failure: the rows are
        still `waiting` and still due, so the next sweep claims them again.
        Nothing is lost, and nothing is expired twice — `expire` carries
        `status = 'waiting'` in its predicate.
        """
        now = self._clock.now()
        claimed = await self._claim(now=now, limit=limit, claimed_by=claimed_by)
        if not claimed:
            return ExpirySweep(claimed=0, expired=0)

        try:
            async with self._unit_of_work:
                resolved = await self._tickets.expire([ticket.id for ticket in claimed], at=now)
                for ticket in claimed:
                    await self._events.publish(
                        QueueTicketExpired(
                            # The ticket's own deadline, not the sweep's
                            # instant — see `QueueTicketExpired` on why the
                            # outbox's ordering depends on it.
                            occurred_at=ticket.expires_at,
                            ticket_id=ticket.id,
                            player_id=ticket.player_id,
                            variant=ticket.pool.variant,
                            queue_type=ticket.queue_type,
                            region=ticket.region,
                            waited_for_seconds=_waited(ticket, ticket.expires_at),
                        )
                    )
                await self._unit_of_work.commit()
        except Exception as error:  # noqa: BLE001 — a background sweep must not escalate
            # Nothing is lost: the tickets are still `waiting` and still
            # due, so the next tick claims them again. `ERROR` because a
            # sweep that cannot record means the queue's terminal state is
            # silently not being written.
            logger.error(
                "queue_expiry_failed",
                extra={"claimed": len(claimed), "error": type(error).__name__},
                exc_info=error,
            )
            return ExpirySweep(claimed=len(claimed), expired=0)

        # A64-014.1's "queue expired". One line for the batch rather than
        # one per ticket: a deploy that leaves two hundred tickets due would
        # otherwise emit two hundred identical records and bury whatever
        # else was happening (CLAUDE.md §8.8).
        logger.info(
            "queue_expired",
            extra={"claimed": len(claimed), "expired": resolved, "worker_id": claimed_by},
        )
        return ExpirySweep(claimed=len(claimed), expired=resolved)

    async def _claim(self, *, now: datetime, limit: int, claimed_by: str) -> Sequence[QueueTicket]:
        """The claim's own transaction, committed immediately so the lock is
        visible to every other sweeper before any event is written."""
        async with self._unit_of_work:
            claimed = await self._tickets.claim_due(now=now, limit=limit, claimed_by=claimed_by)
            await self._unit_of_work.commit()
        return claimed


def _waited(ticket: QueueTicket, until: datetime) -> float:
    """How long the ticket was in the pool, in seconds.

    Computed here rather than on the aggregate: it is a *reporting* figure
    for an event payload and a log line, and `QueueTicket` has no business
    knowing what a consumer wants to plot. Seconds as a float, because the
    interesting range spans a fast cancel and a ten-minute expiry.
    """
    return (until - ticket.entered_at).total_seconds()
