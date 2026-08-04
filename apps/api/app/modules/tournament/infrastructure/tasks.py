"""`tournament`'s scheduled work — SPEC-TOURNAMENT §2, A64-019.2 §9,
A64-019.5.

Two jobs, both `platform.tasks.TaskHandler`, both on the **maintenance**
pool (AD-20) and both idempotent:

    tournament.registration.close_overdue  the deadline is a promise to
                                           players; this keeps it
    tournament.bracket.reconcile           a match `game` has and this
                                           module does not, or the reverse

They are separate handlers rather than one sweep with two halves, because
they claim different rows on different intervals and a slow reconciliation
must not delay a registration close.

## The scheduled close

`registration_deadline` is a promise to players: registration closes when
it is reached, without an operator being awake. This is what keeps it.

## Why a task rather than a timer

AD-21's argument, unchanged: an in-process timer lives on one node, and a
node that is deployed takes every timer it held with it — those tournaments
then never close, they hang. A task claims from the database, so any worker
can do it and a restart loses nothing.

## Idempotent by predicate, not by bookkeeping

The claim is "open **and** past its deadline". A tournament already closed
does not match, so a second worker — or a second run of the same one —
finds nothing and does nothing. There is no ledger to keep and no marker to
set, which is what makes this safe to schedule rather than something to
coordinate.

`FOR UPDATE SKIP LOCKED` is the other half: a tournament another worker is
mid-close on is one this worker should leave alone rather than wait for.
"""

import logging
from collections.abc import Callable, Mapping
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.tournament.application.services.reconciliation_service import (
    TournamentReconciliationService,
)
from app.modules.tournament.application.services.registration_service import (
    TournamentDeadlineService,
)
from app.platform.tasks import TaskRequest

logger = logging.getLogger(__name__)

#: The name `PeriodicTaskScheduler` dispatches and this handler answers to.
DEADLINE_TASK: Final = "tournament.registration.close_overdue"

#: A64-019.5. The recovery for the window BE-05 leaves between `game`
#: committing a match and this module recording the attempt.
RECONCILIATION_TASK: Final = "tournament.bracket.reconcile"

#: AD-20's pool. **`maintenance`**, not `realtime`: a registration that
#: closes a few seconds late costs nobody a game, and sharing the realtime
#: pool would let this sweep delay a clock adjudication.
MAINTENANCE_QUEUE: Final = "maintenance"

DeadlineServiceFactory = Callable[[AsyncSession], TournamentDeadlineService]

ReconciliationServiceFactory = Callable[[AsyncSession], TournamentReconciliationService]


def deadline_request() -> TaskRequest:
    """The request that asks for one sweep."""
    return TaskRequest(name=DEADLINE_TASK, queue=MAINTENANCE_QUEUE)


def reconciliation_request() -> TaskRequest:
    """The request that asks for one reconciliation pass."""
    return TaskRequest(name=RECONCILIATION_TASK, queue=MAINTENANCE_QUEUE)


class TournamentDeadlineTask:
    """`platform.tasks.TaskHandler` — one sweep, over one session."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service_factory: DeadlineServiceFactory,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory

    @property
    def name(self) -> str:
        return DEADLINE_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload — there is nothing to parameterise.

        A request carrying a batch size would be a schedule that could
        disagree with its own configuration, which is the argument every
        other periodic request on this platform makes.
        """
        async with self._session_factory() as session:
            await self._service_factory(session).close_overdue()


class TournamentReconciliationTask:
    """`platform.tasks.TaskHandler` — one reconciliation pass, one session.

    A task rather than something the consumer does on the way past, for
    AD-21's reason and one of its own: the drift this repairs is created by
    a worker **dying**, so the thing that repairs it cannot be the same
    worker. It has to be driven by a schedule that outlives any single
    process.
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
        """Ignores the payload. The batch size is the service's.

        A request carrying one would be a schedule that could disagree with
        its own configuration — the argument every other periodic request on
        this platform makes.
        """
        async with self._session_factory() as session:
            await self._service_factory(session).reconcile_once()


__all__ = [
    "DEADLINE_TASK",
    "MAINTENANCE_QUEUE",
    "RECONCILIATION_TASK",
    "DeadlineServiceFactory",
    "ReconciliationServiceFactory",
    "TournamentDeadlineTask",
    "TournamentReconciliationTask",
    "deadline_request",
    "reconciliation_request",
]
