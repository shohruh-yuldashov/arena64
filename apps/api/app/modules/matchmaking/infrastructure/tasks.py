"""`matchmaking`'s background work — four `platform.tasks` handlers.

`QueueExpiryTask` is the background half of `expires_at`; `PairingTask`
(A64-015.3) scans one pool for a match; `PairingReconciliationTask`
(A64-015.4) recovers the pairings that did not finish; `QueueRetentionTask`
(A64-015.5) lets go of the history none of them owes anybody. All four are
dispatched by a `PeriodicTaskScheduler` and wired at the composition root,
and all four are four lines of body over a service that takes ports — which
is the whole point of AD-17's seam.

Three run on the `matchmaking` queue and the fourth on `maintenance`, which
is AD-20 applied rather than quoted: a prune that is hours late is
invisible, and an expiry sweep that is minutes late leaves players holding
tickets the platform has stopped honouring.

## `QueueExpiryTask` — the background half of `expires_at`

A `platform.tasks.TaskHandler`, dispatched by `PeriodicTaskScheduler` and
wired at the composition root. Without it `expires_at` would still govern
what a player *sees* — `active_ticket` treats a due ticket as absent — but
no ticket would ever reach the `expired` state, no event would be emitted,
and the table would fill with rows that are neither live nor resolved.

## Why this is a task and not a fifth hand-written loop

A64-014.1 asks for a queue dispatch abstraction that Celery can later
replace, and this is its first real user in a bounded context. The
separation it buys is visible in this file's size: the *schedule* is
`PeriodicTaskScheduler`'s, the *routing* is the dispatcher's, and what is
left here is "build a service over a session and call one method". Moving
matchmaking's background work onto a Celery worker replaces the first two
and touches neither this class nor `QueueService`.

## Why infrastructure and not application

It holds a session factory and a service factory — lifetime and wiring, not
a use case — which is the same line `SessionScopedNotificationHandler`
draws. `QueueService.expire_due` is the use case, and it takes ports.

## One session per run

The same reason `OutboxWorker` opens one per tick: a session held between
runs holds a connection idle for the whole interval, and a sweep is a no-op
on most of them.
"""

import logging
import os
import socket
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.matchmaking.application.services import (
    ChallengeExpiryService,
    PairingReconciliationService,
    PairingService,
    QueueRetentionService,
    QueueService,
)
from app.modules.matchmaking.domain.queue_pool import QueuePool
from app.platform.tasks import TaskRequest

logger = logging.getLogger(__name__)

#: The name `PeriodicTaskScheduler` dispatches and this handler answers to.
#: Namespaced by owning context, exactly as every `event_type` is.
QUEUE_EXPIRY_TASK = "matchmaking.queue.expire"

#: The queue this work is routed to once queues exist (AD-20).
#:
#: Its own SLO class rather than `maintenance`, and the distinction is
#: AD-20's own: a prune may be hours late and nobody notices, while an
#: expiry sweep that falls behind leaves players holding tickets the
#: platform has stopped honouring. Sharing a pool with the retention job
#: would let a long prune delay it, which is the interference separate
#: queues exist to make structurally impossible.
MATCHMAKING_QUEUE = "matchmaking"

#: What the composition root supplies: a service over one session.
#:
#: A factory rather than a built service, for the reason
#: `SweeperFactory` takes one: this module knows about *lifetime*, and
#: assembling a `QueueService` means naming a repository, a rating provider,
#: a presence adapter, an event publisher and a unit of work — which is the
#: composition root's job, and would be four boundary violations here.
QueueServiceFactory = Callable[[AsyncSession], QueueService]


def expiry_request() -> TaskRequest:
    """The request that asks for one expiry sweep.

    An empty payload: the batch size is configuration and the instant is
    the service's clock. A request carrying a cutoff would let a stale
    schedule sweep against yesterday's `now`, which on the one job that
    writes terminal states is a way to expire tickets that are not due.
    """
    return TaskRequest(name=QUEUE_EXPIRY_TASK, queue=MATCHMAKING_QUEUE)


def worker_identity() -> str:
    """Hostname and pid, for the `queue_expired` log line.

    Diagnostic only — correctness comes from `FOR UPDATE SKIP LOCKED`, not
    from knowing who claimed what. Unlike the outbox's, this identifier
    reaches no column: see `SqlAlchemyQueueRepository` on why a relation
    whose rows live ten minutes does not carry a `claimed_by`.
    """
    return f"{socket.gethostname()}:{os.getpid()}"[:64]


class QueueExpiryTask:
    """`platform.tasks.TaskHandler` — one expiry sweep, over one session."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service_factory: QueueServiceFactory,
        batch_size: int,
        worker_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory
        self._batch_size = batch_size
        self._worker_id = worker_id or worker_identity()

    @property
    def name(self) -> str:
        return QUEUE_EXPIRY_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload — see `expiry_request` on why there is none.

        Does not catch: `QueueService.expire_due` already records its own
        failures and never raises, so a `try` here would be a second
        swallow with nothing left to swallow. Anything that did escape is a
        failure of the session or the wiring, and
        `InlineTaskDispatcher.dispatch` is where a task's failure is
        recorded — putting a second handler for it here would mean two
        places log the same event.
        """
        async with self._session_factory() as session:
            await self._service_factory(session).expire_due(
                limit=self._batch_size, claimed_by=self._worker_id
            )


#: The name a pairing scan is dispatched under — A64-015.3.
PAIRING_TASK = "matchmaking.queue.pair"

#: The payload key carrying which pool to scan.
#:
#: A `QueuePool.identifier()` and nothing else. §13 forbids serialising a
#: repository or a framework object into a payload, and a pool is already a
#: primitive string by design — see `QueuePool.from_identifier`, which is
#: the other half of this round trip.
PAIRING_POOL_KEY = "pool"

#: What the composition root supplies: a pairing service over one session.
PairingServiceFactory = Callable[[AsyncSession], PairingService]


def pairing_request(pool: QueuePool) -> TaskRequest:
    """The request that asks for one scan of one pool.

    One pool per request, which is A64-015.3 §1 and §12 stated as a wire
    format: a task that carried a list would be a task whose failure is
    partial, and a task that carried none would have to discover its own
    work — putting pool enumeration inside the thing that scans.
    """
    return TaskRequest(
        name=PAIRING_TASK,
        queue=MATCHMAKING_QUEUE,
        payload={PAIRING_POOL_KEY: pool.identifier()},
    )


class PairingTask:
    """`platform.tasks.TaskHandler` — one pairing scan, over one session.

    The same shape as `QueueExpiryTask` beside it, and the same division:
    the *schedule* is `PeriodicTaskScheduler`'s, the *routing* is the
    dispatcher's, and what is left here is "build a service over a session
    and call one method". Moving matchmaking's background work onto Celery
    replaces the first two and touches neither this class nor
    `PairingService`.

    ## It is scheduled since A64-015.4

    `MATCHMAKING_PAIRING_ENABLED` was `False` for one task, because `game`
    could not persist a match and every scan would have reserved two
    tickets, been refused, and released them several times a second
    forever. `PersistentMatchCreation` is what changed, and the setting's
    docstring records the five things that had to be true first.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service_factory: PairingServiceFactory,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory

    @property
    def name(self) -> str:
        return PAIRING_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Scans the one pool the payload names.

        The pool is parsed rather than trusted: a malformed identifier
        raises out of `QueuePool.from_identifier` and is recorded by
        `InlineTaskDispatcher.dispatch`, which is where a task's failure is
        logged. Defaulting to some pool instead would scan the wrong queue
        quietly, which is worse than a loud dispatcher error for a payload
        only this repository's own scheduler produces.

        Does not catch anything else: `PairingService.pair_once` records
        its own failures and never raises, so a `try` here would have
        nothing left to swallow.
        """
        pool = QueuePool.from_identifier(str(payload[PAIRING_POOL_KEY]))
        async with self._session_factory() as session:
            await self._service_factory(session).pair_once(pool=pool)


#: The name a reconciliation pass is dispatched under — A64-015.4 §9.
RECONCILIATION_TASK = "matchmaking.pairing.reconcile"

#: What the composition root supplies: a reconciler over one session.
ReconciliationServiceFactory = Callable[[AsyncSession], PairingReconciliationService]


def reconciliation_request() -> TaskRequest:
    """The request that asks for one reconciliation pass.

    An empty payload, for the reason `expiry_request` carries none: the
    batch size is configuration and the instant is the service's clock. A
    request carrying a cutoff would let a stale schedule reconcile against
    yesterday's `now`, which on the one job that rewrites a pairing's
    outcome is a way to release reservations that are still live.

    **Pool-blind**, unlike `pairing_request`. A stranded reservation is a
    stranded reservation whatever pool it came from, and one worker draining
    every pool is the same shape `QueueExpiryTask` already has — a pass per
    pool would be fourteen mostly-empty ticks for one that finds anything.
    """
    return TaskRequest(name=RECONCILIATION_TASK, queue=MATCHMAKING_QUEUE)


class PairingReconciliationTask:
    """`platform.tasks.TaskHandler` — one reconciliation pass, over one
    session.

    The same shape as the two handlers above and the same division: the
    *schedule* is `PeriodicTaskScheduler`'s, the *routing* is the
    dispatcher's, and what is left here is "build a service over a session
    and call one method". A64-015.4 §9 forbids a direct Celery dependency,
    and this file has none — moving matchmaking's background work onto a
    Celery worker replaces the scheduler and the dispatcher and touches
    neither this class nor `PairingReconciliationService`.

    ## Duplicate delivery is safe

    AD-17's contract is at-least-once, so this handler will occasionally run
    twice for one scheduled tick. Every write the service makes is a
    compare-and-set on the status it read, and the claim underneath is
    `SKIP LOCKED`, so the second run claims what the first left and finds
    nothing to do — see `PairingReconciliationService` on why running it
    twice is running it once.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service_factory: ReconciliationServiceFactory,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory

    @property
    def name(self) -> str:
        return RECONCILIATION_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload — see `reconciliation_request` on why there
        is none.

        Does not catch: `PairingReconciliationService.reconcile_once`
        records its own failures and never raises, so a `try` here would be
        a second swallow with nothing left to swallow.
        """
        async with self._session_factory() as session:
            await self._service_factory(session).reconcile_once()


#: The name a retention run is dispatched under — A64-015.5 §8.
QUEUE_RETENTION_TASK = "matchmaking.queue.prune"

#: The queue this work is routed to once queues exist (AD-20).
#:
#: `maintenance`, not `matchmaking`, and the split is AD-20's own: an
#: expiry sweep that falls behind leaves players holding tickets the
#: platform has stopped honouring, while a prune may be hours late and
#: nobody notices. Sharing a pool would let a long prune delay the sweep,
#: which is precisely the interference separate queues exist to prevent.
#: `OutboxRetentionTask` is routed the same way for the same reason.
MAINTENANCE_QUEUE = "maintenance"

#: What the composition root supplies: a retention service over one session.
QueueRetentionServiceFactory = Callable[[AsyncSession], QueueRetentionService]


def queue_retention_request() -> TaskRequest:
    """The request that asks for one retention run.

    An empty payload, for the reason `expiry_request` and `prune_request`
    both carry none: the horizons are configuration and the instant is the
    service's clock. A request carrying a cutoff would let a stale schedule
    prune against yesterday's horizon, which on the one job that deletes
    anything means deleting more than the policy allows.
    """
    return TaskRequest(name=QUEUE_RETENTION_TASK, queue=MAINTENANCE_QUEUE)


class QueueRetentionTask:
    """`platform.tasks.TaskHandler` — one retention run, over one session.

    The same shape as the three handlers above and the same division: the
    *schedule* is `PeriodicTaskScheduler`'s, the *routing* is the
    dispatcher's, and what is left here is "build a service over a session
    and call one method". A64-015.5 §8 forbids a direct Celery dependency,
    and this file has none.

    ## Duplicate delivery is safe

    AD-17's contract is at-least-once, so this will occasionally run twice
    for one scheduled tick. Every delete is `SELECT ... FOR UPDATE SKIP
    LOCKED` followed by a delete by primary key, so a second run claims what
    the first left and finds nothing — and deleting a row that is already
    gone is not an error, it is an empty batch.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service_factory: QueueRetentionServiceFactory,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory

    @property
    def name(self) -> str:
        return QUEUE_RETENTION_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload — see `queue_retention_request`.

        Does not catch: `QueueRetentionService.prune_once` records its own
        failures and never raises, so a `try` here would be a second
        swallow with nothing left to swallow.
        """
        async with self._session_factory() as session:
            await self._service_factory(session).prune_once()


#: The name a friend challenge expiry sweep is dispatched under — A64-022.6 §2.
CHALLENGE_EXPIRY_TASK = "matchmaking.challenge.expire"

#: What the composition root supplies: an expiry service over one session.
ChallengeExpiryServiceFactory = Callable[[AsyncSession], ChallengeExpiryService]


def challenge_expiry_request() -> TaskRequest:
    """The request that asks for one challenge expiry sweep.

    Routed to `maintenance`, not `matchmaking`, and the split is the one
    `QUEUE_RETENTION_TASK` already draws. AD-20 separates work by what a
    delay costs: a queue expiry that falls behind leaves players holding
    tickets the platform has stopped honouring, where a late challenge
    expiry is a *record* written a minute later than it might have been.
    Nothing waits on it — the recipient already cannot answer an overdue
    challenge and already cannot see it.

    An empty payload, for the reason every request in this file carries
    none: the batch size is configuration and the instant is the service's
    clock. A request carrying a cutoff would let a stale schedule sweep
    against yesterday's, which on this job means expiring challenges that
    are still live.
    """
    return TaskRequest(name=CHALLENGE_EXPIRY_TASK, queue=MAINTENANCE_QUEUE)


class ChallengeExpiryTask:
    """`platform.tasks.TaskHandler` — one challenge sweep, over one session.

    The same shape as the four handlers above and the same division: the
    *schedule* is `PeriodicTaskScheduler`'s, the *routing* is the
    dispatcher's, and what is left here is "build a service over a session
    and call one method".

    ## Duplicate delivery is safe

    AD-17's contract is at-least-once, so this will occasionally run twice
    for one scheduled tick. The second run's claim excludes every row the
    first moved — `claim_expired`'s predicate is `status = 'pending'` — so
    it finds an empty batch, transitions nothing and publishes nothing.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service_factory: ChallengeExpiryServiceFactory,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory

    @property
    def name(self) -> str:
        return CHALLENGE_EXPIRY_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload — see `challenge_expiry_request`.

        Does not catch: `ChallengeExpiryService.expire_due` records its own
        failures and never raises, so a `try` here would be a second swallow
        with nothing left to swallow.
        """
        async with self._session_factory() as session:
            await self._service_factory(session).expire_due()
