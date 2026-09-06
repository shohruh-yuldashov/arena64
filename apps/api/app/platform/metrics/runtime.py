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

from dataclasses import dataclass
from functools import lru_cache

from app.platform.metrics.aggregation import AggregatingMetrics
from app.platform.metrics.ports import Labels, LoggingMetrics, MetricsRecorder
from app.platform.metrics.prometheus import PrometheusMetrics


@dataclass(frozen=True, slots=True)
class FanOutMetrics:
    """Every measurement to each sink, in order — A64-028.6 §5.

    The log line and the exporter want the same measurements for different
    readers: a log is what an incident is reconstructed from afterwards, a
    series is what an alert fires on at the time. Neither replaces the other,
    and duplicating the call sites to feed both would be the "one concept,
    two spellings" defect this module was written to end.

    A sink that raises must not stop the sinks after it — the port's
    never-raises contract is per implementation, and this composes them.
    """

    sinks: tuple[MetricsRecorder, ...]

    def increment(self, name: str, *, labels: Labels | None = None, by: int = 1) -> None:
        for sink in self.sinks:
            sink.increment(name, labels=labels, by=by)

    def observe(self, name: str, value: float, *, labels: Labels | None = None) -> None:
        for sink in self.sinks:
            sink.observe(name, value, labels=labels)


@lru_cache(maxsize=1)
def prometheus_metrics() -> PrometheusMetrics:
    """The process's exporter.

    Separate from `process_metrics()` because two callers need it for
    something other than recording: the `/metrics` route renders it, and the
    composition root registers gauge sources against it.
    """
    return PrometheusMetrics()


@lru_cache(maxsize=1)
def process_metrics() -> AggregatingMetrics:
    """The process-wide recorder: aggregating counters over a logging sink.

    Returns the **concrete class** rather than `MetricsRecorder`, uniquely on
    this platform, because `MetricsFlushTask` needs `flush()` and a port must
    not grow a method that exists for one implementation's lifecycle. Every
    other caller should annotate the port and stay ignorant of this.

    ## What aggregation costs the exporter, and why it is accepted

    Counters reach both sinks through `AggregatingMetrics`, so a counter's
    total is exact but arrives in steps of the flush interval; observations
    already pass straight through, so latency histograms are live. Set
    `APP_METRICS_FLUSH_INTERVAL_SECONDS` at or below the scrape interval —
    the production compose sets both to 15 — or `rate()` will read the
    steps rather than the traffic.

    Aggregating in front of the exporter as well as the log is deliberate:
    the alternative is a second recorder feeding Prometheus directly, and a
    second recorder is precisely the defect the rest of this docstring is
    about.
    """
    return AggregatingMetrics(sink=FanOutMetrics((LoggingMetrics(), prometheus_metrics())))


__all__ = ["FanOutMetrics", "process_metrics", "prometheus_metrics"]
