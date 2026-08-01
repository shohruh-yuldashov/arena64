"""`PresenceSweeperWorker` — the loop that ticks the sweeper.

The same shape as `OutboxWorker`: wait, build over a fresh session, tick,
repeat; never exit on an error.

## Why this is a second loop rather than a shared abstraction

It is visibly parallel to `OutboxWorker._run`, and extracting a
`PeriodicWorker` was considered and deliberately not done. CLAUDE.md §1.7:
"abstractions are earned by a third concrete use case, not predicted by the
first." There are two periodic jobs, and they already differ in what matters —
the relay owns a session per tick and passes it to a graph of repositories,
while this one needs a session only to write outbox rows.

The third periodic job (AD-21's clock worker is the obvious candidate) earns
the abstraction, and at that point the shared piece is the ten lines of
cancellation handling rather than the loop as a whole. Recorded in A64-013.8's
technical debt so the decision is visible rather than rediscovered.

## Why a session per tick

The same reason the relay opens one: a session held across ticks holds a
connection idle between them, and a sweep is usually a no-op. Building it
inside the tick means an idle sweeper holds nothing.

## Cooperative shutdown, not cancellation

`stop()` sets an event and waits for the current tick to finish; it cancels
only if that takes longer than a grace period. Cancelling first was the
obvious implementation and produced a real defect: the task was cancelled
*inside* `async with session`, so the session's cleanup ran under
cancellation while `lifespan` went on to dispose the engine — surfacing as
`Future exception was never retrieved: ConnectionError('unexpected
connection_lost() call')` on every shutdown. Caught by a startup smoke run,
not by a test, which is why the smoke run is worth doing.

Waiting is safe because a tick is bounded: one Redis read, one bounded batch
of inserts, one commit. The cancel is the backstop for the case where it is
not — a hung connection — and a shutdown must not be hostage to that either.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.notifications.application.services.presence_sweeper import PresenceSweeper

logger = logging.getLogger(__name__)

#: How long `stop` waits for an in-flight tick before cancelling it. A tick is
#: one Redis read plus one bounded batch of inserts, so five seconds is many
#: times the normal case and short enough that a deploy is never held up by a
#: sweeper that has stopped responding.
_SHUTDOWN_GRACE_SECONDS = 5.0

#: What the composition root supplies: a sweeper over one session.
#:
#: A factory rather than a built sweeper, for the reason
#: `SessionScopedNotificationHandler` takes one: this module knows about
#: *lifetime*, and assembling a sweeper means naming an event publisher, a
#: unit of work and a presence adapter — which is the composition root's job
#: and, when it was done here, was three boundary violations.
SweeperFactory = Callable[[AsyncSession], PresenceSweeper]


class PresenceSweeperWorker:
    """Runs `PresenceSweeper.sweep_once` on an interval until stopped."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        sweeper_factory: SweeperFactory,
        interval_seconds: float,
    ) -> None:
        self._session_factory = session_factory
        self._sweeper_factory = sweeper_factory
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """Schedules the loop. Idempotent — starting twice is a no-op."""
        if self._task is not None:
            return

        self._task = asyncio.create_task(self._run(), name="presence-sweeper")
        logger.info(
            "presence_sweeper_started",
            extra={"interval_seconds": self._interval_seconds},
        )

    async def stop(self) -> None:
        """Asks the loop to finish, and waits for it — see the module docstring.

        The event wakes it out of its sleep immediately; a tick already in
        flight completes, which is what keeps its session's cleanup out of a
        cancellation. `SHUTDOWN_GRACE_SECONDS` bounds the wait so a hung
        connection cannot hold a deploy open, and only then is the task
        cancelled.
        """
        task, self._task = self._task, None
        if task is None:
            return

        self._stopping.set()
        try:
            await asyncio.wait_for(task, timeout=_SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            logger.warning(
                "presence_sweeper_stop_timed_out",
                extra={"grace_seconds": _SHUTDOWN_GRACE_SECONDS},
            )
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        logger.info("presence_sweeper_stopped")

    async def sweep_once(self) -> None:
        """One tick, over one session. Public so a test can drive the sweeper
        without a timer, and so an operator can force one from a script."""
        async with self._session_factory() as session:
            await self._sweeper_factory(session).sweep_once()

    async def _run(self) -> None:
        """**Never exits on an error**, only when asked to stop.

        `sweep_once` already swallows and records its own failures, so
        anything reaching here is a failure of the machinery itself — the
        database being unreachable, most likely. A worker that exited on the
        first connection blip would need a human to notice, and departures
        would silently stop being announced until somebody did.
        """
        while not self._stopping.is_set():
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — the loop must outlive its failures
                logger.error(
                    "presence_sweep_tick_failed",
                    extra={"error": type(error).__name__},
                    exc_info=error,
                )

            # `wait_for` on the stop event rather than `sleep`: a shutdown
            # must not wait out a fifteen-second interval before it is
            # noticed. The timeout *is* the interval.
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)
