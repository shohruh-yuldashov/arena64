"""Background task dispatch — the seam AD-17 pre-paid for, A64-014.1.

    ports.py       `TaskRequest`, `TaskDispatcher`, `TaskHandler`
    inline.py      the dispatcher this build runs — in-process, awaited
    scheduler.py   the beat: one request, on an interval

A caller that wants work done off the request path builds a `TaskRequest`
and hands it to a `TaskDispatcher`. It cannot tell whether the work ran in
this event loop or on another machine, which is the whole point: AD-17
names Celery as the platform's asynchronous execution framework and says
the migration is "only the dispatch adapter is replaced". Until this
package, nothing on the platform dispatched anything, so that claim could
not be checked.

**Celery is still not a dependency of this build.** Adding one is outside a
task's authority (CLAUDE.md §11), and there is nothing yet that a broker
would do better than a coroutine. What is here is the shape that makes
adding it a wiring change: see `InlineTaskDispatcher` for the four lines
`CeleryTaskDispatcher` replaces it with.
"""

from app.platform.tasks.inline import InlineTaskDispatcher, UnknownTask
from app.platform.tasks.ports import (
    DEFAULT_QUEUE,
    TaskDispatcher,
    TaskHandler,
    TaskRequest,
)
from app.platform.tasks.scheduler import PeriodicTaskScheduler

__all__ = [
    "DEFAULT_QUEUE",
    "InlineTaskDispatcher",
    "PeriodicTaskScheduler",
    "TaskDispatcher",
    "TaskHandler",
    "TaskRequest",
    "UnknownTask",
]
