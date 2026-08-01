"""Outbox retention — A64-014.1, and the bound A64-013.7 shipped without.

AD-16 makes an event as durable as the fact that caused it, and AD-17 goes
further: the outbox table *is* the durable event log, "retained and
re-dispatchable", which is why it can give up stream replay. Nothing in
either says *forever*, and forever is what the first implementation did.

    OutboxEntry     "Retained, never deleted — AD-17 makes this table the
                    durable event log projections rebuild from"

That sentence is now qualified by a horizon. What retention buys, and what
it costs, stated plainly:

  **Buys** a bounded relation. CLAUDE.md §10.5 requires every unbounded
  thing to be bounded, and DB-18 already names this the platform's
  highest-churn relation. At the projected volume — one row per social
  event today, one per move and one per completed match tomorrow — an
  unpruned outbox is the largest table on the platform inside a year, and
  the first symptom is the relay's own index no longer fitting in cache.

  **Costs** the ability to re-dispatch an event older than the horizon. A
  projection rebuilt from events alone could then only go back that far —
  which is why AD-19 requires every projection to be rebuildable from
  PostgreSQL rather than from the log, and why the horizon is a *setting*
  rather than a constant: a platform that discovers it needs ninety days
  raises a number instead of writing a migration.

## Two horizons, and why the ledger's is the longer one

`platform.processed_event` records that a consumer handled an event id, and
it is what makes at-least-once delivery safe. Dropping a ledger row while
its outbox entry can still be claimed would let that entry be redelivered
*and* re-handled — the exact double-effect the ledger exists to prevent.

So the ledger's horizon is held at or beyond the outbox's, and the pruner
deletes outbox rows first within a run. `RetentionPolicy` refuses to
construct otherwise: this is an ordering invariant, not a tuning
preference, and a settings file is the wrong place to discover it.

## Why a task rather than a method somebody remembers to call

`OutboxRetentionTask` is a `platform.tasks.TaskHandler`, dispatched by
`PeriodicTaskScheduler`. That indirection is not ceremony — it is the
property AD-17 asks for, applied to the first job that would otherwise have
been a fifth hand-written loop: the schedule and the work are separable, so
moving retention onto Celery beat is a wiring change here and nothing else.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.database.unit_of_work import SessionUnitOfWork
from app.platform.outbox.ports import OutboxRetentionStore
from app.platform.outbox.repository import SqlAlchemyOutboxRetentionStore
from app.platform.tasks import TaskRequest

logger = logging.getLogger(__name__)

#: The name `PeriodicTaskScheduler` dispatches and `OutboxRetentionTask`
#: answers to. Namespaced by owner, like every `event_type` on the platform.
OUTBOX_PRUNE_TASK = "platform.outbox.prune"

#: The queue this work is routed to once queues exist (AD-20).
#:
#: Named `maintenance` rather than `default` because that is the SLO class
#: it belongs to — minutes to hours, never on a path anybody waits for. A
#: retention job sharing a pool with the clock worker is AD-20's worked
#: example of what must not happen.
MAINTENANCE_QUEUE = "maintenance"


def prune_request() -> TaskRequest:
    """The request that asks for one prune.

    An empty payload: the horizons are configuration and the instant is the
    handler's clock. A request carrying a cutoff would let a stale schedule
    dispatch yesterday's horizon, which is a way to delete more than the
    policy allows from the one job that deletes anything.
    """
    return TaskRequest(name=OUTBOX_PRUNE_TASK, queue=MAINTENANCE_QUEUE)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How much history the outbox keeps, and how fast it may let go.

    Frozen and validated at construction (DI-06's posture applied to a
    policy): a retention rule that is wrong is discovered when the data is
    already gone, so the checks belong before the first delete rather than
    in a review.
    """

    published_retention: timedelta
    """How long a delivered entry is kept, measured on `occurred_at`.

    On `occurred_at` rather than `published_at` because that is DB-18's
    partition key — see `OutboxRetentionStore.prune_published`.
    """

    ledger_retention: timedelta
    """How long a `processed_event` row is kept, measured on
    `processed_at`. At or beyond `published_retention` — see this module's
    docstring on why that ordering is an invariant."""

    batch_size: int
    """Rows per `DELETE`. Bounds the lock one statement takes."""

    max_batches: int
    """Batches per run. Bounds the whole job.

    Both bounds exist because the interesting case is the *first* run after
    this ships, or the first after an incident: a year of retained rows
    would otherwise be one job holding locks on the highest-churn relation
    until it finished. Draining over several runs is slower and is never an
    incident.
    """

    def __post_init__(self) -> None:
        if self.published_retention <= timedelta(0):
            raise ValueError("published_retention must be positive")
        if self.ledger_retention < self.published_retention:
            raise ValueError(
                "ledger_retention must be at least published_retention — a ledger row "
                "dropped while its outbox entry can still be claimed lets that entry be "
                "redelivered and re-handled"
            )
        if self.batch_size < 1 or self.max_batches < 1:
            raise ValueError("batch_size and max_batches must be positive")


@dataclass(frozen=True, slots=True)
class PruneResult:
    """What one run did. Returned rather than only logged, so a test asserts
    on the outcome and the job logs it once."""

    entries_deleted: int
    ledger_deleted: int

    retained_unpublished: int
    """Entries past the horizon that were **kept** because they are still
    unpublished.

    The number that says why the floor did not move — and, once DB-18's
    partitions exist, why the oldest one cannot be detached. Zero in a
    healthy platform; anything else is an event nobody has delivered and
    nobody has noticed.
    """

    @property
    def is_idle(self) -> bool:
        return self.entries_deleted == 0 and self.ledger_deleted == 0


class OutboxPruner:
    """Deletes what the outbox no longer owes anybody.

    Holds ports only — a store, a unit of work and a clock — so it is
    testable with no database and no timer, which is the point of AD-06.
    """

    def __init__(
        self,
        *,
        store: OutboxRetentionStore,
        unit_of_work: UnitOfWork,
        clock: Clock,
        policy: RetentionPolicy,
    ) -> None:
        self._store = store
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._policy = policy

    async def prune_once(self) -> PruneResult:
        """One bounded run. Never raises.

        A retention job that propagated an exception would stop the schedule
        that called it — the same argument `OutboxRelay.run_once` makes —
        and a retention job that has silently stopped is invisible until the
        table it was bounding is the incident.

        **One transaction per batch, not one per run.** Each batch commits
        on its own so the locks it took are released before the next is
        taken; holding them across twenty batches would reproduce the
        unbounded `DELETE` this design exists to avoid.
        """
        now = self._clock.now()
        entry_cutoff = now - self._policy.published_retention
        ledger_cutoff = now - self._policy.ledger_retention

        try:
            entries = await self._drain(
                lambda batch: self._store.prune_published(before=entry_cutoff, batch_size=batch)
            )
            # **Outbox first, ledger second**, within the run as well as in
            # the horizons. See this module's docstring: the ordering is
            # what keeps a redeliverable entry from outliving the record
            # that its consumer already handled it.
            ledger = await self._drain(
                lambda batch: self._store.prune_processed_events(
                    before=ledger_cutoff, batch_size=batch
                )
            )
            async with self._unit_of_work:
                retained = await self._store.unpublished_before(entry_cutoff)
                await self._unit_of_work.commit()
        except Exception as error:  # noqa: BLE001 — a maintenance job must not escalate
            logger.error(
                "outbox_prune_failed",
                extra={"error": type(error).__name__},
                exc_info=error,
            )
            return PruneResult(entries_deleted=0, ledger_deleted=0, retained_unpublished=0)

        if retained:
            # `WARNING`, and it names no event: these rows are the backlog
            # that `OutboxEntry` deliberately keeps visible, and by now they
            # are older than the whole retention horizon — which means
            # nothing has delivered them for weeks.
            logger.warning(
                "outbox_retention_blocked",
                extra={"retained_unpublished": retained, "horizon": entry_cutoff.isoformat()},
            )

        logger.info(
            "outbox_prune_completed",
            extra={
                "entries_deleted": entries,
                "ledger_deleted": ledger,
                "retained_unpublished": retained,
            },
        )
        return PruneResult(
            entries_deleted=entries, ledger_deleted=ledger, retained_unpublished=retained
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


class OutboxRetentionTask:
    """`platform.tasks.TaskHandler` — one prune, over one session.

    Owns a session *factory* rather than a session, exactly as `OutboxWorker`
    does and for the same reason: a session held between runs holds a
    connection idle for an hour, and a prune is a no-op on most of them.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        policy: RetentionPolicy,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._policy = policy
        self._clock = clock

    @property
    def name(self) -> str:
        return OUTBOX_PRUNE_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload — see `prune_request` on why there is none."""
        async with self._session_factory() as session:
            pruner = OutboxPruner(
                store=SqlAlchemyOutboxRetentionStore(session),
                unit_of_work=SessionUnitOfWork(session),
                clock=self._clock,
                policy=self._policy,
            )
            await pruner.prune_once()


def retention_policy(
    *,
    published_retention_days: int,
    ledger_retention_days: int,
    batch_size: int,
    max_batches: int,
) -> RetentionPolicy:
    """A policy from `OutboxSettings`' flat integers.

    Here rather than as a method on the settings class, so `app/config/`
    keeps holding configuration and this module keeps owning what the
    numbers *mean* — including the ordering invariant, which is enforced by
    `RetentionPolicy.__post_init__` and would be a second copy of a rule if
    it were also checked in settings.
    """
    return RetentionPolicy(
        published_retention=timedelta(days=published_retention_days),
        ledger_retention=timedelta(days=ledger_retention_days),
        batch_size=batch_size,
        max_batches=max_batches,
    )
