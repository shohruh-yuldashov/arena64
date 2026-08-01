"""`QueueExpiryTask` — the background half of `expires_at`.

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

from app.modules.matchmaking.application.services import QueueService
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
