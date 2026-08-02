"""`QueueRetentionService` — bounding the two relations `matchmaking` owns.
A64-015.5 §8.

A64-014.1 shipped `queue_ticket` with the gap recorded in its own model
docstring: "**resolved tickets are retained without a horizon** … storage
grows with matches attempted, forever … The fix is
`platform.outbox.retention`'s, applied to this table." This is that fix, and
it deliberately reuses the shape rather than the code — see below.

## Five relations, five reasons, one job

    queue_ticket    terminal rows. Kept for a while because "why was I
                    matched with them" is a question somebody asks the day
                    after, and answered from `entered_at`, the pool and the
                    rating snapshot.
    queue_cooldown  lapsed rows. Kept for **no** time at all beyond the
                    horizon, because a cooldown that has lifted answers no
                    question anybody will ask.
    game.match      cancelled and expired pairings — matches that never
                    became games. `game` owns the rows and publishes the
                    sweep (`AbandonedMatchRetention`); the *horizon* is the
                    same product judgement as the queue's, so the module
                    with the opinion supplies it.

A64-015.6 adds the two audit relations, and both keep a **longer** horizon
than the operational rows they describe — which is the whole point of an
audit trail:

    queue_cooldown_audit  why a player was barred. Outlives the bar, because
                          the dispute arrives after it has lifted (§3)
    pairing_timeline      what recovery did to a ticket. Outlives the
                          ticket, because the question is asked about a
                          ticket that is gone (§4)

All five in one pass rather than five tasks, because they are one job —
"let go of what this handshake no longer owes anybody" — and because
configuring three horizons that must stay consistent in three places is how
they stop being consistent. Three schedules would also be three things to
turn off during an incident, and an operator who turned off two of them
would have a relation growing silently.

A match that *was* played is untouched by any horizon here. See
`game.public.AbandonedMatchRetention`: `active` is excluded by predicate,
not by configuration.

## Why not `platform.outbox.RetentionPolicy` itself

It was considered and is the wrong reuse. That policy carries an ordering
invariant specific to the outbox (`ledger_retention >= published_retention`,
because dropping a ledger row while its entry is claimable causes a double
effect) which has no meaning here, and it would have to grow a second set of
fields for two relations that do not have that relationship. Sharing the
*shape* — a horizon, a bounded batch, a run ceiling, `SKIP LOCKED` — and not
the type is what CLAUDE.md §2.7 asks for: "duplication is cheaper than the
wrong abstraction".

## The horizon has a floor, and the floor is reconciliation

A retention horizon shorter than the window in which a stranded pairing can
still be recovered would delete the evidence recovery reads. The settings
validator holds `MATCHMAKING_TICKET_RETENTION_HOURS` well above the
reservation TTL and the ticket TTL for that reason, and the *predicate* is
the real guarantee: `resolved_at IS NOT NULL` cannot reach a live ticket
however the horizon is configured. See `SqlAlchemyQueueRetentionStore`.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.game.public import AbandonedMatchRetention
from app.modules.matchmaking.application.metrics import (
    RETENTION_DELETIONS,
    RetentionRelation,
)
from app.modules.matchmaking.application.ports import (
    CooldownAuditRepository,
    CooldownRepository,
    QueueRetentionStore,
    ReconciliationTimelineRepository,
)
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QueueRetentionPolicy:
    """How much queue history this module keeps, and how fast it may let go.

    Frozen and validated at construction — DI-06's posture applied to a
    policy, and for the reason `RetentionPolicy` gives: a retention rule
    that is wrong is discovered when the data is already gone, so the checks
    belong before the first delete rather than in a review.
    """

    ticket_retention: timedelta
    """How long a **terminal** ticket is kept, measured on `resolved_at`."""

    abandoned_match_retention: timedelta
    """How long a **cancelled or expired** match is kept, measured on
    `settled_at`.

    Longer than the ticket horizon by default, and the asymmetry is
    deliberate: "why was I matched with them" is answered from a queue
    ticket and asked within a day, while "why did my opponent decline"
    is answered from a match and is the question a support conversation
    starts with a week later.
    """

    cooldown_audit_retention: timedelta
    """How long a **cooldown audit row** is kept, measured on `applied_at`.

    Much longer than the bar it describes, and that asymmetry is the reason
    the relation exists: a player disputing a cooldown does so after it has
    lifted, and the enforcement row is pruned within the hour.
    """

    timeline_retention: timedelta
    """How long a **reconciliation timeline entry** is kept, measured on
    `occurred_at`.

    Bounded by what it is a projection *of*: the outbox entries it was built
    from are pruned on `OUTBOX_RETENTION_DAYS`, and keeping the derivative
    longer than the source would leave a timeline nothing could rebuild
    (AD-19).
    """

    cooldown_retention: timedelta
    """How long a **lapsed** cooldown row is kept past its own expiry.

    Short by design and not zero. A cooldown read that is in flight when the
    row expires would otherwise race the delete, and the read's answer
    ("no active cooldown") is the same either way — so the margin buys
    nothing except the absence of a class of confusing `NoResultFound`.
    """

    batch_size: int
    """Rows per statement. Bounds the lock one delete takes."""

    max_batches: int
    """Batches per run. Bounds the whole job.

    The interesting case is the **first** run after this ships: a year of
    resolved tickets would otherwise be one job holding locks on the queue
    relation until it finished. Draining over several runs is slower and is
    never an incident.
    """

    def __post_init__(self) -> None:
        if self.ticket_retention <= timedelta(0):
            raise ValueError("ticket_retention must be positive")
        if self.abandoned_match_retention <= timedelta(0):
            raise ValueError("abandoned_match_retention must be positive")
        if self.cooldown_retention < timedelta(0):
            raise ValueError("cooldown_retention cannot be negative")
        if self.cooldown_audit_retention <= self.cooldown_retention:
            raise ValueError(
                "cooldown_audit_retention must exceed cooldown_retention — an audit "
                "trail pruned before the thing it explains answers nothing"
            )
        if self.timeline_retention <= timedelta(0):
            raise ValueError("timeline_retention must be positive")
        if self.batch_size < 1 or self.max_batches < 1:
            raise ValueError("batch_size and max_batches must be positive")


@dataclass(frozen=True, slots=True)
class QueueRetentionResult:
    """What one run did. Returned rather than only logged, so a test asserts
    on the outcome and the job logs it once — the shape `PruneResult`,
    `ExpirySweep` and `ReconciliationOutcome` already use."""

    tickets_deleted: int
    matches_deleted: int
    cooldowns_deleted: int
    cooldown_audits_deleted: int
    timeline_entries_deleted: int

    live_tickets_past_horizon: int
    """Live tickets older than the whole retention horizon, which were
    **kept**.

    Zero on a healthy platform, and a genuine alarm otherwise: a `waiting`
    ticket that old means the expiry sweep has stopped, and a `reserved` one
    means reconciliation has. Both are silent failures, and this is the
    number that makes them loud — the same job `retained_unpublished` does
    for the outbox.
    """

    unresolved_matches_past_horizon: int
    """Matches older than the horizon that are still `pending_acceptance`.

    The match-side twin of `live_tickets_past_horizon`, and the same kind of
    alarm: two players holding an offer whose deadline passed days ago means
    the acceptance-expiry sweep has stopped."""

    @property
    def is_idle(self) -> bool:
        return (
            self.tickets_deleted == 0 and self.matches_deleted == 0 and self.cooldowns_deleted == 0
        )


class QueueRetentionService:
    """Deletes the queue history nobody owes anybody.

    Holds ports only — a retention store, the cooldown store, a unit of
    work, a clock and a metrics recorder — so it is testable with no
    database and no timer.

    It deliberately does **not** hold a `QueueRepository`: the object that
    can delete a ticket must not also be able to resolve one, which is the
    same split `OutboxRetentionStore` makes against `OutboxRepository`.
    """

    def __init__(
        self,
        *,
        tickets: QueueRetentionStore,
        matches: AbandonedMatchRetention,
        cooldowns: CooldownRepository,
        cooldown_audit: CooldownAuditRepository,
        timeline: ReconciliationTimelineRepository,
        unit_of_work: UnitOfWork,
        clock: Clock,
        metrics: MetricsRecorder,
        policy: QueueRetentionPolicy,
    ) -> None:
        self._tickets = tickets
        self._matches = matches
        self._cooldowns = cooldowns
        self._cooldown_audit = cooldown_audit
        self._timeline = timeline
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._metrics = metrics
        self._policy = policy

    async def prune_once(self) -> QueueRetentionResult:
        """One bounded run. Never raises.

        A retention job that propagated would stop the schedule that called
        it — the same argument `OutboxPruner.prune_once` makes — and a
        retention job that has silently stopped is invisible until the table
        it was bounding is the incident.

        **One transaction per batch, not one per run.** Each batch commits
        on its own so the locks it took are released before the next is
        taken; holding them across twenty batches would reproduce the
        unbounded `DELETE` this design exists to avoid.
        """
        now = self._clock.now()
        ticket_cutoff = now - self._policy.ticket_retention
        match_cutoff = now - self._policy.abandoned_match_retention
        cooldown_cutoff = now - self._policy.cooldown_retention
        audit_cutoff = now - self._policy.cooldown_audit_retention
        timeline_cutoff = now - self._policy.timeline_retention

        try:
            tickets = await self._drain(
                lambda batch: self._tickets.prune_resolved(before=ticket_cutoff, batch_size=batch)
            )
            matches = await self._drain(
                lambda batch: self._matches.prune_abandoned(before=match_cutoff, batch_size=batch)
            )
            cooldowns = await self._drain(
                lambda batch: self._cooldowns.prune_expired(
                    before=cooldown_cutoff, batch_size=batch
                )
            )
            audits = await self._drain(
                lambda batch: self._cooldown_audit.prune_recorded(
                    before=audit_cutoff, batch_size=batch
                )
            )
            timeline = await self._drain(
                lambda batch: self._timeline.prune_recorded(
                    before=timeline_cutoff, batch_size=batch
                )
            )
            async with self._unit_of_work:
                stranded = await self._tickets.live_before(ticket_cutoff)
                await self._unit_of_work.commit()
            unresolved = await self._matches.unsettled_before(match_cutoff)
        except Exception as error:  # noqa: BLE001 — a maintenance job must not escalate
            logger.error(
                "queue_retention_failed",
                extra={"error": type(error).__name__},
                exc_info=error,
            )
            return QueueRetentionResult(
                tickets_deleted=0,
                matches_deleted=0,
                cooldowns_deleted=0,
                cooldown_audits_deleted=0,
                timeline_entries_deleted=0,
                live_tickets_past_horizon=0,
                unresolved_matches_past_horizon=0,
            )

        # Every relation the run pruned, including the ones it deleted
        # nothing from. A series that exists with a value of zero says "the
        # job ran and found nothing"; a series that is absent says "the job
        # did not run", and telling those apart is the entire operational
        # value of a retention metric — see `AggregatingMetrics.increment` on
        # why `by=0` is recorded rather than skipped.
        for relation, deleted in (
            (RetentionRelation.QUEUE_TICKET, tickets),
            (RetentionRelation.ABANDONED_MATCH, matches),
            (RetentionRelation.QUEUE_COOLDOWN, cooldowns),
            (RetentionRelation.COOLDOWN_AUDIT, audits),
            (RetentionRelation.PAIRING_TIMELINE, timeline),
        ):
            self._metrics.increment(RETENTION_DELETIONS, labels={"relation": relation}, by=deleted)

        if stranded:
            # `WARNING`, and it names no player: these are live tickets
            # older than the entire retention horizon, which means a sweep
            # that should have resolved them days ago has not run.
            logger.warning(
                "queue_retention_blocked",
                extra={
                    "live_tickets_past_horizon": stranded,
                    "horizon": ticket_cutoff.isoformat(),
                },
            )

        if unresolved:
            logger.warning(
                "match_retention_blocked",
                extra={
                    "unresolved_matches_past_horizon": unresolved,
                    "horizon": match_cutoff.isoformat(),
                },
            )

        logger.info(
            "queue_retention_completed",
            extra={
                "tickets_deleted": tickets,
                "matches_deleted": matches,
                "cooldowns_deleted": cooldowns,
                "cooldown_audits_deleted": audits,
                "timeline_entries_deleted": timeline,
                "live_tickets_past_horizon": stranded,
                "unresolved_matches_past_horizon": unresolved,
            },
        )
        return QueueRetentionResult(
            tickets_deleted=tickets,
            matches_deleted=matches,
            cooldowns_deleted=cooldowns,
            cooldown_audits_deleted=audits,
            timeline_entries_deleted=timeline,
            live_tickets_past_horizon=stranded,
            unresolved_matches_past_horizon=unresolved,
        )

    async def _drain(self, delete_batch: Callable[[int], Awaitable[int]]) -> int:
        """Runs bounded batches until one comes back short, or the run's
        ceiling is reached.

        A short batch means the horizon is caught up, which is the ordinary
        steady state and costs exactly one empty statement per run. The
        ceiling is what stops a first run against years of history from
        being unbounded after all.
        """
        deleted = 0
        for _ in range(self._policy.max_batches):
            async with self._unit_of_work:
                removed = await delete_batch(self._policy.batch_size)
                await self._unit_of_work.commit()

            deleted += removed
            if removed < self._policy.batch_size:
                break
        return deleted


def queue_retention_policy(
    *,
    ticket_retention_hours: int,
    abandoned_match_retention_hours: int,
    cooldown_retention_hours: int,
    cooldown_audit_retention_hours: int,
    timeline_retention_hours: int,
    batch_size: int,
    max_batches: int,
) -> QueueRetentionPolicy:
    """A policy from `MatchmakingSettings`' flat integers.

    Here rather than as a method on the settings class, so `app/config/`
    keeps holding configuration and this module keeps owning what the
    numbers *mean* — the same division `retention_policy` makes for the
    outbox.
    """
    return QueueRetentionPolicy(
        ticket_retention=timedelta(hours=ticket_retention_hours),
        abandoned_match_retention=timedelta(hours=abandoned_match_retention_hours),
        cooldown_retention=timedelta(hours=cooldown_retention_hours),
        cooldown_audit_retention=timedelta(hours=cooldown_audit_retention_hours),
        timeline_retention=timedelta(hours=timeline_retention_hours),
        batch_size=batch_size,
        max_batches=max_batches,
    )


__all__ = [
    "QueueRetentionPolicy",
    "QueueRetentionResult",
    "QueueRetentionService",
    "queue_retention_policy",
]
