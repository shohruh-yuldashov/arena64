"""`MetricsFlushTask` — the schedule that drains `AggregatingMetrics`.

A `platform.tasks.TaskHandler`, dispatched by `PeriodicTaskScheduler` and
wired at the composition root, exactly like `OutboxRetentionTask` beside it.
Four lines of body over an accumulator, which is the whole point of AD-17's
seam: the *schedule* is the scheduler's, the *routing* is the dispatcher's,
and what is left here is "drain the thing".

## Why a task rather than a timer inside the recorder

A recorder that started its own `asyncio` task would be a piece of
infrastructure with a lifecycle nobody registered — it would keep running
after `lifespan` tore its process down, and it could not be stopped by an
operator. Every other periodic job on this platform is a `TaskHandler` for
that reason, and this one has no argument for being the exception.

It also means the flush is **observable and testable**: a test calls `run`
and asserts what was emitted, with no clock and no sleeping.

## What a missed flush costs

Counts accumulated since the last one, held in memory. On a clean shutdown
that is up to one interval of counters lost, and on a crash the same — which
is the correct trade for a metric and would not be for an event. The outbox
exists for the things that must survive; this is a count of how often a scan
found nobody.
"""

import logging
from collections.abc import Mapping
from typing import Any

from app.platform.metrics.aggregation import AggregatingMetrics
from app.platform.tasks import TaskRequest

logger = logging.getLogger(__name__)

#: The name `PeriodicTaskScheduler` dispatches and this handler answers to.
#: Namespaced by owner, like every task and every `event_type`.
METRICS_FLUSH_TASK = "platform.metrics.flush"

#: The queue this work is routed to once queues exist (AD-20).
#:
#: `maintenance`, with the outbox pruner and the queue's retention: a flush
#: that is a minute late costs a minute of resolution on a dashboard, and a
#: flush that shares a pool with the pairing scan would let a slow prune delay
#: a match. That is the interference separate queues exist to prevent.
MAINTENANCE_QUEUE = "maintenance"


def flush_request() -> TaskRequest:
    """The request that asks for one flush.

    An empty payload, for the reason every other periodic request on this
    platform carries none: there is nothing to parameterise, and a request
    that carried the accumulator would be a request nobody could serialise
    onto a broker.
    """
    return TaskRequest(name=METRICS_FLUSH_TASK, queue=MAINTENANCE_QUEUE)


class MetricsFlushTask:
    """`platform.tasks.TaskHandler` — one drain of the process's counters.

    Holds the **accumulator itself** rather than a factory, which is the
    opposite of every other task on this platform and is right for the same
    reason they are not: the others hold a session factory because a session
    must not outlive its unit of work, and this holds one object because the
    counters *are* process state. A per-run accumulator would flush an empty
    dictionary forever.
    """

    def __init__(self, *, metrics: AggregatingMetrics) -> None:
        self._metrics = metrics

    @property
    def name(self) -> str:
        return METRICS_FLUSH_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Drains the accumulator. Ignores the payload.

        Never raises, and the `except` is deliberate rather than defensive:
        `MetricsRecorder`'s contract is that a metric never changes
        behaviour, and a flush that propagated would stop the schedule that
        drains it — turning a broken sink into permanently lost counters
        rather than one lost interval.
        """
        try:
            emitted = self._metrics.flush()
        except Exception as error:  # noqa: BLE001 — a metric must never change behaviour
            logger.error(
                "metrics_flush_failed",
                extra={"error": type(error).__name__},
                exc_info=error,
            )
            return

        if emitted:
            logger.debug("metrics_flushed", extra={"series": emitted})


__all__ = ["METRICS_FLUSH_TASK", "MetricsFlushTask", "flush_request"]
