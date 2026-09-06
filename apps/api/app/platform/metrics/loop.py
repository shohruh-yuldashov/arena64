"""Event-loop lag, measured inside the API process — A64-028.6 §4.

## Why the load harness's number was not this number

A64-028.5A reported a loop lag of 1.07–2.08 ms p50 and said, in the
document, that it was the **generator's** loop and not the server's. That
was honest and it was also useless for operating the platform: the client
being healthy says nothing about whether the server's loop is blocked, and a
blocked loop is the single failure mode that makes an asyncio service stop
answering while every dependency it has is fine.

## What it measures

`asyncio.sleep(interval)` and the difference between how long that took and
how long it asked for. The loop can only return late, and it returns late
by exactly as long as it was busy elsewhere — so the drift *is* the lag.
`perf_counter` because it is monotonic: a clock adjustment during a
measurement would otherwise read as a stall.

## Why it costs nothing

One coroutine, one wake-up per interval, one subtraction, one observation.
At the 1-second default that is 60 measurements a minute against a process
handling hundreds of requests a second — far below the noise it is
measuring. The interval is deliberately not configurable downwards past a
bound: a probe that wakes every few milliseconds becomes the load it is
trying to detect.

## Lifecycle

Started and stopped by the lifespan like every other background task, and
`stop()` waits for the task to end rather than only cancelling it, so a
process that is shutting down cannot leave a probe writing into a recorder
whose sink is closing. Starting twice is refused rather than tolerated —
two probes would double every reading and the second would be invisible.
"""

import asyncio
import contextlib
import logging
import time
from typing import Final

from app.platform.metrics.ports import MetricsRecorder

logger = logging.getLogger(__name__)

#: Seconds. Histogram, because the question is "how often is it bad", which
#: a mean cannot answer — one 400 ms stall a minute is an outage for the
#: requests it lands on and invisible in an average.
LOOP_LAG: Final = "process.event_loop_lag_seconds"


class EventLoopLagProbe:
    """Samples this process's scheduling drift on a fixed interval."""

    def __init__(self, *, metrics: MetricsRecorder, interval_seconds: float = 1.0) -> None:
        if interval_seconds < 0.1:
            # A guard rather than a clamp: a caller asking for 10 ms wants
            # something this cannot give, and silently giving them 100 ms
            # would be worse than saying so.
            raise ValueError(
                f"interval_seconds={interval_seconds} is below the 0.1 floor — a probe that "
                "wakes faster than that measures itself."
            )
        self._metrics = metrics
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("The event-loop lag probe is already running.")
        self._task = asyncio.create_task(self._run(), name="metrics:event-loop-lag")
        logger.info("event_loop_probe_started", extra={"interval_seconds": self._interval})

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("event_loop_probe_stopped")

    async def _run(self) -> None:
        while True:
            before = time.perf_counter()
            await asyncio.sleep(self._interval)
            # Clamped at zero: `sleep` may return a hair early on some
            # platforms, and a negative lag is not a thing.
            self._metrics.observe(LOOP_LAG, max(0.0, time.perf_counter() - before - self._interval))


__all__ = ["LOOP_LAG", "EventLoopLagProbe"]
