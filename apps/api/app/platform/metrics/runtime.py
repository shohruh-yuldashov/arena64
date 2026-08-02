"""`process_metrics()` — the one recorder a process has. A64-015.6 §10.

## The gap this closes

A64-015.5 wired metrics twice and nobody noticed, because both spellings
worked. The composition root built one recorder for the worker paths, and
`matchmaking.presentation.dependencies.get_metrics` built another for the
request paths — and A64-015.6 §6 turned that from redundancy into a defect:

    worker path    AggregatingMetrics  counters summed, one record per flush
    request path   LoggingMetrics      one record per measurement

Same metric names, two emission policies. `matchmaking.acceptance_*` is
incremented on both paths, so a dashboard summing it was summing a flushed
delta and a per-call record together — and the accumulated half simply did
not exist for any series only the request path touched.

Aggregation also makes the recorder **stateful**, which is what turns "two
instances" from waste into lost data: counters live in whichever instance
took them, and `MetricsFlushTask` drains exactly one.

## Why here rather than at the composition root

`app_factory` is the composition root and would be the natural home — but
`get_metrics` is a FastAPI dependency inside `matchmaking`, and a module
importing the composition root inverts the layering (`.importlinter` would
be right to refuse it). `app/platform` is below both, imports no module, and
already owns the recorder itself.

So the *choice* of sink stays one function, and both callers reach it
downward. `engine_services()` is the same shape and the same reasoning.

## Cached rather than a module global

`lru_cache` over a bare `_INSTANCE = ...`, for the reason `engine_services`
gives: the sharing is visible at the call site rather than at import, and a
test can `process_metrics.cache_clear()` to get a process with no history —
which is not possible with a global and matters here, because this one
carries counters between tests otherwise.
"""

from functools import lru_cache

from app.platform.metrics.aggregation import AggregatingMetrics
from app.platform.metrics.ports import LoggingMetrics


@lru_cache(maxsize=1)
def process_metrics() -> AggregatingMetrics:
    """The process-wide recorder: aggregating counters over a logging sink.

    Returns the **concrete class** rather than `MetricsRecorder`, uniquely on
    this platform, because `MetricsFlushTask` needs `flush()` and a port must
    not grow a method that exists for one implementation's lifecycle. Every
    other caller should annotate the port and stay ignorant of this.
    """
    return AggregatingMetrics(sink=LoggingMetrics())


__all__ = ["process_metrics"]
