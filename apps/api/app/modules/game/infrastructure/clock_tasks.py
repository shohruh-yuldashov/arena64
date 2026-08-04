"""The clock worker's task adapter — AD-17, AD-21. A64-016.5 §6.

A `platform.tasks.TaskHandler`, dispatched by `PeriodicTaskScheduler` and
wired at the composition root, exactly like `QueueExpiryTask` and
`OutboxRetentionTask` beside it. §6 requires "no direct Celery dependency"
and this is how the platform has met that since A64-013.5: the *schedule* is
the scheduler's, the *routing* is the dispatcher's, and what is left here is
"open a session and run one pass".

## Why it holds a factory rather than a service

Same reason `OutboxRetentionTask` does: a session held between runs holds a
connection idle for the whole interval, and adjudication is a no-op on most
passes — the common case is that nobody's flag has fallen.

## How often it should run

`GAME_CLOCK_INTERVAL_SECONDS`, default one second. That is the resolution of
the flag, not a tuning knob: a player whose time runs out is told so within
the interval, and on a bullet game an interval of ten seconds would let them
keep moving for nine of them.

One second across a fleet is one `ZRANGEBYSCORE` per worker per second
against an index that is empty when nothing is expiring — the same cost the
outbox relay already pays at the same interval, and the same argument
`OUTBOX_POLL_INTERVAL_SECONDS` records.
"""

import logging
from collections.abc import Callable, Mapping
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.game.application.services.clock_adjudication_service import (
    ClockAdjudicationService,
)
from app.platform.tasks import TaskRequest

logger = logging.getLogger(__name__)

#: The name `PeriodicTaskScheduler` dispatches and this handler answers to.
#: Namespaced by owner, like every task and every `event_type`.
CLOCK_ADJUDICATION_TASK: Final = "game.clock.adjudicate"

#: The queue this work is routed to once queues exist (AD-20).
#:
#: **`realtime`**, not `maintenance`, and the distinction is AD-20's own:
#: workers are separated by service-level objective. A flag that is a minute
#: late is a game decided wrongly; a retention sweep that is a minute late is
#: nothing. Sharing a pool would let a slow prune delay a timeout.
REALTIME_QUEUE: Final = "realtime"

#: What the composition root supplies: an adjudication service over one
#: session. A factory rather than an instance, so the session's lifetime is
#: the run's — see this module's docstring.
AdjudicationFactory = Callable[[AsyncSession], ClockAdjudicationService]


def adjudication_request() -> TaskRequest:
    """The request that asks for one adjudication pass.

    An empty payload, like every other periodic request on this platform:
    there is nothing to parameterise, and a request carrying a batch size
    would be a schedule that could disagree with its own configuration.
    """
    return TaskRequest(name=CLOCK_ADJUDICATION_TASK, queue=REALTIME_QUEUE)


class ClockAdjudicationTask:
    """`platform.tasks.TaskHandler` — one adjudication pass, over one
    session."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service_factory: AdjudicationFactory,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory

    @property
    def name(self) -> str:
        return CLOCK_ADJUDICATION_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload — see `adjudication_request`.

        Does not catch: `adjudicate_once` records its own failures and never
        raises, so a `try` here would be a second swallow with nothing left
        to swallow. Anything that escapes is a failure of the session or the
        wiring, which `InlineTaskDispatcher.dispatch` already records.
        """
        async with self._session_factory() as session:
            await self._service_factory(session).adjudicate_once()


__all__ = [
    "CLOCK_ADJUDICATION_TASK",
    "REALTIME_QUEUE",
    "AdjudicationFactory",
    "ClockAdjudicationTask",
    "adjudication_request",
]
