"""`OutboxWorker` — the loop that ticks the relay.

Deliberately thin: sleep, build a relay over a fresh session, `run_once`,
repeat. Everything that can be got wrong about *delivery* is in
`OutboxRelay`, which has no timer and no task; everything that can be got
wrong about *lifetime* is here, and there is not much of it.

## Why this is an asyncio task and not a Celery worker

AD-17 names Celery as the platform's event transport, and this is not that.
Celery is not a dependency of this build — adding one is outside a task's
authority (CLAUDE.md §11) — and more importantly AD-17's *contract* is about
the transport between the relay and its subscribers, which A64-013.7 does
not need: there is exactly one subscriber, it is in-process, and the only
transport it would use is a function call.

So this is the shape AD-17 anticipates as "only the relay's dispatch adapter
is replaced". What is already correct for that day and would be expensive to
retrofit is the part that is here: a claim that is safe for N workers
(`SKIP LOCKED`), a per-consumer idempotency ledger, and bounded retry on the
row rather than in a broker. When the dispatch becomes
`task.apply_async(...)`, the table and everything above it are unchanged.

## One process today, N tomorrow

The worker takes a `worker_id` and writes it to `claimed_by`. Nothing
depends on it being unique — correctness comes from the row lock — but an
operator looking at a stuck row wants to know which process was holding it,
and a hostname-and-pid is the cheapest honest answer.

## In-process with the API, and what that costs

Running the relay inside the API process means a deploy that restarts the
API also restarts the relay, and a relay under load competes with request
handling for the event loop. Both are acceptable at this size and neither is
acceptable at production volume, which is why `OUTBOX_WORKER_ENABLED` exists
as a per-process switch: the deployment shape is one API tier with it off
and one small worker tier with it on, running the same image.
"""

import asyncio
import logging
import os
import socket
from collections.abc import Sequence
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import OutboxSettings
from app.core.clock import Clock
from app.database.unit_of_work import SessionUnitOfWork
from app.platform.outbox.ports import EventHandler
from app.platform.outbox.relay import OutboxRelay
from app.platform.outbox.repository import (
    SqlAlchemyOutboxRepository,
    SqlAlchemyProcessedEventStore,
)

logger = logging.getLogger(__name__)


def worker_identity() -> str:
    """Hostname and pid, truncated to the column's width.

    Diagnostic only — see this module's docstring. Truncated rather than
    hashed so it stays readable in a `psql` session, which is the only place
    it is ever looked at.
    """
    return f"{socket.gethostname()}:{os.getpid()}"[:64]


class OutboxWorker:
    """Polls the outbox until stopped.

    Owns the session factory rather than a session: a session held across
    ticks would hold a connection idle between them and would accumulate a
    transaction's worth of state that nothing resets. One session per tick,
    closed at the end of it, is the same lifetime a request gets.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        handlers: Sequence[EventHandler],
        settings: OutboxSettings,
        clock: Clock,
        worker_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._handlers = handlers
        self._settings = settings
        self._clock = clock
        self._worker_id = worker_id or worker_identity()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Starts the polling task. Idempotent — starting twice is a no-op.

        Returns as soon as the task is scheduled: `lifespan` must not block
        on a loop that never ends.
        """
        if self._task is not None:
            return

        self._task = asyncio.create_task(self._run(), name="outbox-relay")
        logger.info(
            "outbox_worker_started",
            extra={
                "worker_id": self._worker_id,
                "poll_interval_seconds": self._settings.poll_interval_seconds,
                "batch_size": self._settings.batch_size,
            },
        )

    async def stop(self) -> None:
        """Cancels the task and waits for it to unwind.

        Awaited rather than fired and forgotten, because the task holds a
        database session: returning from `lifespan` while it is mid-tick
        would tear the engine down underneath an open connection, which
        surfaces as an unrelated-looking error during every shutdown.
        """
        task, self._task = self._task, None
        if task is None:
            return

        task.cancel()
        # Suppressed rather than handled: the cancellation *is* the stop, and
        # letting it propagate would push a `CancelledError` into
        # `lifespan`'s shutdown path, where nothing asked to be cancelled.
        with suppress(asyncio.CancelledError):
            await task

        logger.info("outbox_worker_stopped", extra={"worker_id": self._worker_id})

    async def run_once(self) -> None:
        """One tick, over one session. Public so a test can drive the worker
        without a timer, and so an operator can trigger a drain from a
        script.
        """
        async with self._session_factory() as session:
            relay = OutboxRelay(
                outbox=SqlAlchemyOutboxRepository(session),
                processed=SqlAlchemyProcessedEventStore(session),
                handlers=self._handlers,
                unit_of_work=SessionUnitOfWork(session),
                clock=self._clock,
                worker_id=self._worker_id,
                batch_size=self._settings.batch_size,
                max_attempts=self._settings.max_attempts,
                retry_base_seconds=self._settings.retry_base_seconds,
                retry_max_seconds=self._settings.retry_max_seconds,
            )
            await relay.run_once()

    async def _run(self) -> None:
        """The loop. **Never exits on an error**, only on cancellation.

        `OutboxRelay.run_once` already records per-entry failures on their
        rows, so anything reaching here is a failure of the machinery
        itself — the database being unreachable, most likely. That is a
        condition to log and retry, not one to stop delivering events over:
        a worker that exited on the first connection blip would need a human
        to notice and restart it, and the backlog would grow silently until
        somebody did.
        """
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — the loop must outlive its failures
                logger.error(
                    "outbox_tick_failed",
                    extra={"worker_id": self._worker_id, "error": type(error).__name__},
                    exc_info=error,
                )

            await asyncio.sleep(self._settings.poll_interval_seconds)
