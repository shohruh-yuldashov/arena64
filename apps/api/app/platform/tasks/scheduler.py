"""`PeriodicTaskScheduler` — the beat half, and the only timer in this
package.

Dispatches one `TaskRequest` on an interval until stopped. It knows nothing
about what the task does, holds no session, and cannot fail in a way the
task can observe — which is what makes it replaceable by Celery beat
without any handler changing.

## Why a scheduler rather than two more worker loops

A64-014.1 adds two periodic jobs (outbox retention, queue-ticket expiry).
`OutboxWorker` and `PresenceSweeperWorker` are already two hand-written
loops, and `presence_sweeper_worker.py` records the judgement that "the
third periodic job earns the abstraction". These are the third and fourth,
so the abstraction is taken — but only for the *new* loops.

The two existing ones are deliberately left alone. Migrating them is a
refactor, CLAUDE.md §7.3 forbids bundling one with a feature, and neither
would be simplified by this class: `OutboxWorker` builds a repository graph
per tick and `PresenceSweeperWorker` owns a session, while this dispatches
a value. Recorded as a recommendation for A64-014.2 rather than done here.

## Cooperative shutdown, not cancellation

Copied deliberately from `PresenceSweeperWorker`, including the reason,
because getting it wrong produced a real defect there: cancelling a task
mid-tick tears down a database session *under cancellation* while
`lifespan` goes on to dispose the engine, which surfaces as
`ConnectionError('unexpected connection_lost() call')` on every shutdown.
`stop()` therefore asks, waits, and only cancels if the grace period
elapses.

## An interval, not a cron expression

The two jobs this schedules are "every so often", not "at 03:00" — a prune
that runs hourly needs no calendar, and a queue sweep that ran once a night
would be useless. A cron expression would mean a parser and a timezone
question (DM-14) for a requirement nobody has. Celery beat accepts both, so
the migration is unaffected.
"""

import asyncio
import logging
from contextlib import suppress

from app.platform.tasks.ports import TaskDispatcher, TaskRequest

logger = logging.getLogger(__name__)

#: How long `stop` waits for an in-flight dispatch before cancelling it.
#:
#: Matched to `PresenceSweeperWorker`'s, and for the same reason: a tick is
#: bounded work, so five seconds is many times the normal case and short
#: enough that a deploy is never held hostage to a hung connection.
_SHUTDOWN_GRACE_SECONDS = 5.0


class PeriodicTaskScheduler:
    """Dispatches one request on a fixed interval until stopped.

    One scheduler per task. Two jobs with different intervals are two
    instances rather than one object with a list, because a list would make
    a slow task delay the one after it — which is AD-20's shared-queue
    failure reproduced inside a single process.
    """

    def __init__(
        self,
        *,
        dispatcher: TaskDispatcher,
        request: TaskRequest,
        interval_seconds: float,
    ) -> None:
        self._dispatcher = dispatcher
        self._request = request
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """Schedules the loop. Idempotent — starting twice is a no-op.

        **The first dispatch waits one interval.** A prune or a sweep that
        fired the instant a process came up would run on every replica of a
        rolling deploy at once, which is the thundering herd the interval
        exists to spread out.
        """
        if self._task is not None:
            return

        self._task = asyncio.create_task(self._run(), name=f"schedule:{self._request.name}")
        logger.info(
            "task_schedule_started",
            extra={
                "task": self._request.name,
                "queue": self._request.queue,
                "interval_seconds": self._interval_seconds,
            },
        )

    async def stop(self) -> None:
        """Asks the loop to finish and waits for it — see the module
        docstring on why this is not a bare `cancel()`."""
        task, self._task = self._task, None
        if task is None:
            return

        self._stopping.set()
        try:
            await asyncio.wait_for(task, timeout=_SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            logger.warning(
                "task_schedule_stop_timed_out",
                extra={"task": self._request.name, "grace_seconds": _SHUTDOWN_GRACE_SECONDS},
            )
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        logger.info("task_schedule_stopped", extra={"task": self._request.name})

    async def trigger_once(self) -> None:
        """One dispatch, now. Public so a test drives the schedule without a
        timer, and so an operator can force a run from a script."""
        await self._dispatcher.dispatch(self._request)

    async def _run(self) -> None:
        """**Never exits on an error**, only when asked to stop.

        The dispatcher already records a handler's failure, so anything
        reaching here is a failure of the dispatch itself — an unroutable
        name, most likely. A loop that exited on it would need a human to
        notice that a scheduled job had stopped, and nothing about a job
        that quietly stops is visible until the thing it was preventing has
        already happened.
        """
        while not self._stopping.is_set():
            # The wait leads, so nothing fires at startup — see `start`.
            # `wait_for` on the event rather than `sleep`, so a shutdown is
            # not held for a whole interval; the timeout *is* the interval.
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)

            if self._stopping.is_set():
                return

            try:
                await self.trigger_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — the loop must outlive its failures
                logger.error(
                    "task_schedule_tick_failed",
                    extra={"task": self._request.name, "error": type(error).__name__},
                    exc_info=error,
                )
