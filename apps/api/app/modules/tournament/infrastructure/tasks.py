"""The scheduled close — SPEC-TOURNAMENT §2, A64-019.2 §9.

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

from app.modules.tournament.application.services.registration_service import (
    TournamentDeadlineService,
)
from app.platform.tasks import TaskRequest

logger = logging.getLogger(__name__)

#: The name `PeriodicTaskScheduler` dispatches and this handler answers to.
DEADLINE_TASK: Final = "tournament.registration.close_overdue"

#: AD-20's pool. **`maintenance`**, not `realtime`: a registration that
#: closes a few seconds late costs nobody a game, and sharing the realtime
#: pool would let this sweep delay a clock adjudication.
MAINTENANCE_QUEUE: Final = "maintenance"

DeadlineServiceFactory = Callable[[AsyncSession], TournamentDeadlineService]


def deadline_request() -> TaskRequest:
    """The request that asks for one sweep."""
    return TaskRequest(name=DEADLINE_TASK, queue=MAINTENANCE_QUEUE)


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


__all__ = [
    "DEADLINE_TASK",
    "MAINTENANCE_QUEUE",
    "DeadlineServiceFactory",
    "TournamentDeadlineTask",
    "deadline_request",
]
