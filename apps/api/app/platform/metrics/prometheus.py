"""The exporter — A64-028.6 §5.

## Why this exists at all

`platform/metrics/__init__.py` has said since A64-015 that the recorder is a
seam and that an exporter would be "a second implementation of
`MetricsRecorder` wired at the composition root". This is that
implementation. Nothing about the call sites changes: 41 metric names are
already emitted at the right instants with `StrEnum` labels, and A64-028.1
recorded that what was missing was never the instrumentation but the way
out of the process.

## Why `prometheus-client` rather than a hand-written exposition

The text format is simple enough to write by hand, which is the argument
for doing so, and `CLAUDE.md` §2.6 is right that a dependency is a
liability. Two things decided it the other way. Histogram exposition is not
simple — `_bucket` cumulative semantics, `+Inf`, `_sum`, `_count`, and the
escaping rules — and an alert an operator is paged by must not rest on this
repository's own reading of a specification. And the package is pure
Python with **no transitive dependencies**, which is the cheapest a
dependency gets. It is pinned like every other.

## Counters, histograms, and the gauge that was deliberately absent

The port has `increment` and `observe` and no gauge, and the reason given in
`ports.py` is exact: "a gauge is a value read at scrape time, which needs
the exporter to call *into* the application — the one shape that cannot be
expressed as a log line." That was true while the only sink was a log.

It is no longer true, so gauges arrive the way that reasoning implies:
`GaugeSource` is registered with the exporter, and the exporter calls it
**during the scrape**. A backlog depth is not a thing that happened, it is a
thing that is — and asking the database at scrape time is both cheaper and
more truthful than a counter that tries to track it by arithmetic.

## Naming

Metric names on this platform are dotted (`gateway.moves_rejected_total`).
Prometheus permits `[a-zA-Z_:][a-zA-Z0-9_:]*`, so dots become underscores
and everything gains an `arena64_` prefix, which is what makes the platform's
series separable from an exporter's own in a shared instance.

The mapping is total and reversible by inspection; it is not a lookup table
that can drift from the catalogues.

## Restart semantics

Counters are per-process and reset to zero when the process restarts, which
is what every Prometheus counter does and what `rate()` is built to survive.
Scraping is stateless: nothing here writes, and a scrape that fails changes
nothing about the application.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import CollectorRegistry as _Registry

from app.platform.metrics.ports import Labels

logger = logging.getLogger(__name__)

#: Prefix on every series, so the platform's metrics are separable from an
#: exporter's own in a shared Prometheus.
NAMESPACE: Final = "arena64"

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

#: Seconds. Chosen from A64-028.5A's measurements rather than from a
#: default: authenticated reads sat at 20–150 ms p95, a cross-instance frame
#: at 182 ms, a move round trip at 155–2 071 ms across the game ladder, and
#: the 35-minute soak's p99 was 616 ms with a maximum of 1.45 s. Buckets
#: below 25 ms would only resolve an idle machine, and the top of the range
#: has to be well past the worst measurement or the histogram cannot
#: distinguish "slow" from "hung".
LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    float("inf"),
)

#: Seconds. Event-loop lag is a different quantity from a request latency —
#: a healthy loop is under a millisecond and anything past a second means
#: the process has stopped serving — so it gets its own scale rather than
#: sharing one where every healthy reading lands in the first bucket.
LAG_BUCKETS: Final[tuple[float, ...]] = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    float("inf"),
)

_ILLEGAL = re.compile(r"[^a-zA-Z0-9_]")

#: Names whose observations are loop lag rather than request latency.
_LAG_METRICS: Final = frozenset({"process.event_loop_lag_seconds"})

#: A gauge reads a current value at scrape time. It returns label sets to
#: values, so one source can publish a family; the empty mapping key is the
#: unlabelled series.
GaugeSource = Callable[[], Awaitable[Mapping[tuple[tuple[str, str], ...], float]]]


def prometheus_name(name: str) -> str:
    """`gateway.moves_rejected_total` → `arena64_gateway_moves_rejected_total`."""
    return f"{NAMESPACE}_{_ILLEGAL.sub('_', name)}"


class PrometheusMetrics:
    """A `MetricsRecorder` that accumulates into a Prometheus registry.

    **Never raises**, which is the port's contract and matters more here
    than anywhere else: a metric that could fail a request would make
    observability the thing that takes the platform down.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self._registry: _Registry = registry if registry is not None else CollectorRegistry()
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._label_names: dict[str, tuple[str, ...]] = {}
        self._gauges: dict[str, tuple[Gauge, GaugeSource]] = {}
        # Its own series, because a silently dropped measurement is the one
        # failure an operator would otherwise have to infer from a gap.
        self._dropped = Counter(
            f"{NAMESPACE}_metrics_dropped_total",
            "Measurements the exporter refused, by reason.",
            ["reason"],
            registry=self._registry,
        )

    @property
    def registry(self) -> CollectorRegistry:
        return self._registry

    def increment(self, name: str, *, labels: Labels | None = None, by: int = 1) -> None:
        try:
            counter = self._counter_for(name, labels)
            if counter is None:
                return
            (counter.labels(**dict(labels)) if labels else counter).inc(by)
        except Exception:  # noqa: BLE001 — see the class docstring
            self._drop("increment_failed")

    def observe(self, name: str, value: float, *, labels: Labels | None = None) -> None:
        try:
            histogram = self._histogram_for(name, labels)
            if histogram is None:
                return
            (histogram.labels(**dict(labels)) if labels else histogram).observe(value)
        except Exception:  # noqa: BLE001 — see the class docstring
            self._drop("observe_failed")

    def register_gauge(self, name: str, documentation: str, source: GaugeSource) -> None:
        """Publishes a value the exporter reads **at scrape time** — see the
        module docstring on why gauges arrive this way and not through the
        recorder port.

        Registering the same name twice is a composition defect rather than
        a runtime condition, so it replaces rather than accumulating: the
        alternative is two sources writing one series and a number nobody
        can attribute.
        """
        existing = self._gauges.get(name)
        gauge = (
            existing[0]
            if existing is not None
            else Gauge(
                prometheus_name(name),
                documentation,
                ["source"],
                registry=self._registry,
            )
        )
        self._gauges[name] = (gauge, source)

    async def render(self) -> bytes:
        """The exposition, with every gauge read first.

        A gauge source that fails does **not** fail the scrape: its series is
        left at whatever it last held and the failure is counted. A monitoring
        endpoint that returns 500 because one query timed out takes every
        other metric down with it, which is the opposite of what it is for.
        """
        for name, (gauge, source) in self._gauges.items():
            try:
                for key, value in (await source()).items():
                    gauge.labels(source=_label_of(key)).set(value)
            except Exception:  # noqa: BLE001 — a scrape must survive its sources
                logger.warning("metrics_gauge_source_failed", extra={"metric": name})
                self._drop("gauge_source_failed")
        return bytes(generate_latest(self._registry))

    def _counter_for(self, name: str, labels: Labels | None) -> Counter | None:
        if not self._agrees(name, labels):
            return None
        counter = self._counters.get(name)
        if counter is None:
            counter = Counter(
                prometheus_name(name),
                f"Arena64 counter {name}.",
                self._label_names[name],
                registry=self._registry,
            )
            self._counters[name] = counter
        return counter

    def _histogram_for(self, name: str, labels: Labels | None) -> Histogram | None:
        if not self._agrees(name, labels):
            return None
        histogram = self._histograms.get(name)
        if histogram is None:
            histogram = Histogram(
                prometheus_name(name),
                f"Arena64 observation {name}, in seconds.",
                self._label_names[name],
                buckets=LAG_BUCKETS if name in _LAG_METRICS else LATENCY_BUCKETS,
                registry=self._registry,
            )
            self._histograms[name] = histogram
        return histogram

    def _agrees(self, name: str, labels: Labels | None) -> bool:
        """Whether this measurement's labels match the ones the series was
        created with.

        Prometheus fixes a metric's label names when it is declared, and this
        platform's call sites are consistent by construction — one catalogue
        constant, one label set. A disagreement is therefore a defect
        somewhere upstream, and the honest response is to drop the
        measurement and **count the drop**, rather than to raise inside a
        request or to invent a value for the missing label.
        """
        seen = tuple(sorted(labels)) if labels else ()
        known = self._label_names.setdefault(name, seen)
        if known == seen:
            return True
        logger.error(
            "metrics_label_mismatch",
            extra={"metric": name, "declared": list(known), "received": list(seen)},
        )
        self._drop("label_mismatch")
        return False

    def _drop(self, reason: str) -> None:
        # The counter of refusals must not itself be able to refuse.
        with suppress(Exception):
            self._dropped.labels(reason=reason).inc()


def _label_of(key: Sequence[tuple[str, str]]) -> str:
    """A gauge family's key as one `source` label value.

    One label rather than a declared set, because a gauge source knows its
    own dimensions and the exporter does not — and a `Gauge` declared with
    the union of every source's labels would carry empty strings for the
    ones that do not apply.
    """
    return ",".join(f"{name}={value}" for name, value in key) if key else "-"


__all__ = [
    "CONTENT_TYPE",
    "LAG_BUCKETS",
    "LATENCY_BUCKETS",
    "NAMESPACE",
    "GaugeSource",
    "PrometheusMetrics",
    "prometheus_name",
]
