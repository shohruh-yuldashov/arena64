"""`InlineTaskDispatcher` — the dispatcher this build runs, and the one
AD-17 replaces.

It looks up a handler by name and awaits it in the calling event loop.
That is not a stub: it is the honest implementation of "background work"
for a platform whose worker tier is a coroutine inside the API process
(`OutboxWorker`'s docstring makes the same argument about Celery not being
a dependency of this build).

What matters is what it *refuses* to expose, because those are the
properties a caller could otherwise come to depend on and lose on the day
the dispatcher becomes Celery's:

    it returns nothing            so no caller can read a result
    it does not re-raise a
      handler's exception         so no caller can catch one
    it offers no "run now"        so no caller can force ordering

## Why an unknown name raises and a failed handler does not

They are different kinds of wrong. A handler that raised is a transient
failure of the work — the same event a Celery worker would record and
retry, and one that must not propagate into whatever scheduled it. An
unroutable name is a *wiring* defect: the process was started with a
scheduler for a task nobody registered, and every dispatch until somebody
notices is silence. Raising is the only thing that makes that visible.

## The migration, concretely

`CeleryTaskDispatcher.dispatch` is::

    self._celery.send_task(request.name, kwargs=dict(request.payload),
                           queue=request.queue)

and the handlers below become the bodies of Celery tasks registered under
the same names. Nothing above this line changes, which is the claim AD-17
makes and the reason this class exists rather than a direct call.
"""

import logging
from collections.abc import Sequence

from app.core.exceptions import PermanentInfrastructureError
from app.platform.tasks.ports import TaskHandler, TaskRequest

logger = logging.getLogger(__name__)


class UnknownTask(PermanentInfrastructureError):
    """No handler is registered under that name.

    `Permanent`, not transient: retrying a dispatch against a process that
    was started without the handler will fail identically forever. A human
    has to look, which is exactly what this taxonomy member means
    (`app.core.exceptions`).
    """


class InlineTaskDispatcher:
    """Runs dispatched work in this process, in the caller's event loop.

    Built once per process at the composition root, over every handler that
    process is configured to run. It holds no session and no per-request
    state — the handlers own their own resource lifetimes, exactly as
    `OutboxWorker` owns its session factory rather than a session.
    """

    def __init__(self, handlers: Sequence[TaskHandler]) -> None:
        self._handlers: dict[str, TaskHandler] = {}
        for handler in handlers:
            if handler.name in self._handlers:
                # At construction, so a duplicate registration fails at
                # startup rather than silently letting the last one win —
                # DI-06's "abort before accepting traffic", applied to
                # wiring rather than to configuration.
                raise ValueError(f"two handlers registered for task {handler.name!r}")
            self._handlers[handler.name] = handler

    @property
    def registered(self) -> frozenset[str]:
        """Every task name this process can run.

        Exposed so the composition root can log it once at startup: "which
        tasks does this process handle" is the first question during an
        incident where something scheduled is not happening, and the answer
        is otherwise only in the source of whoever built the dispatcher.
        """
        return frozenset(self._handlers)

    async def dispatch(self, request: TaskRequest) -> None:
        """Runs `request` now. See this module's docstring on what that
        deliberately does not promise.

        Failures are logged with the task name and the exception *type* —
        never the payload, which is the same rule the outbox applies to
        `last_error` (A64-013.7: never log a payload).
        """
        handler = self._handlers.get(request.name)
        if handler is None:
            logger.error(
                "task_unroutable",
                extra={"task": request.name, "queue": request.queue},
            )
            raise UnknownTask(f"No handler is registered for task {request.name!r}.")

        logger.debug("task_dispatched", extra={"task": request.name, "queue": request.queue})
        try:
            await handler.run(request.payload)
        except Exception as error:  # noqa: BLE001 — a task's failure is not its caller's
            logger.error(
                "task_failed",
                extra={
                    "task": request.name,
                    "queue": request.queue,
                    "error": type(error).__name__,
                },
                exc_info=error,
            )
