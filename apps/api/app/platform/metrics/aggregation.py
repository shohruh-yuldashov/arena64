"""`AggregatingMetrics` — one record per series per interval, instead of one
per measurement. A64-015.6 §6.

A64-015.5 shipped `LoggingMetrics`, which emits a structured log record every
time anything is counted. That was right for what existed: every metric on the
platform was per-match or per-run, so the volume was the volume of business
events. A64-015.6 §7 asks for **pairing-scan** metrics, and the scan is a
different kind of caller — `MATCHMAKING_PAIRING_INTERVAL_SECONDS` is one
second and `every_pool()` returns fourteen pools, so a single naive counter on
that path is ~1.2 million log records a day per process, for a platform with
no players on it.

The reconciler is already doing a smaller version of the same thing: its
`no_action` counter fires on every tick at five-second intervals, which is
~17,000 records a day of "nothing happened".

## The rule: counters aggregate, observations do not

    increment   accumulated in memory, flushed as one record per series
    observe     passed straight through, one record per measurement

That asymmetry is not a compromise, it is the arithmetic:

**A counter summed over an interval loses nothing.** The sum *is* the counter.
Emitting `pairing_scans_total{outcome=idle} += 840` once a minute carries
exactly the information of 840 separate records, and a rate query over either
returns the same number.

**An observation summed over an interval loses the distribution**, which is
the only thing an observation is for. `game.match_answer_latency_seconds`
exists so that §2's tuning process can read a `p99` and the *shape* of the
tail; a mean and a count cannot answer either question. It is also a
low-frequency metric — one record per match answered — so there is nothing to
save.

So the hot path is bounded and the evidence path keeps full fidelity, which is
the outcome §6 asks for and §2 depends on.

## Memory is bounded by the enums, not by traffic

The accumulator is keyed on `(name, sorted labels)`. Every label value on this
platform comes from a closed `StrEnum` — that is A64-015.5 §9's cardinality
rule, and `tests/unit/test_matchmaking_metrics.py` asserts it — so the number
of live keys is fixed at import time and is currently under forty. There is no
growth term in traffic, which is what makes an in-memory accumulator safe here
and would make it a leak in a system that labelled by identifier.

## Why not a real exporter

A64-015.6 §6 says to prefer Prometheus-compatible metrics "if the project
already has" them. It does not: there is no `prometheus-client`, no StatsD, no
OpenTelemetry collector in `pyproject.toml`, and §6 also says not to introduce
an observability platform in this task. Adding a dependency is outside a
task's authority anyway (CLAUDE.md §11).

What this is, then, is the same seam `LoggingMetrics` already was, with the
volume problem solved: `MetricsRecorder` is unchanged, every call site is
unchanged, and the day an exporter exists it replaces the *sink* this wraps.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.platform.metrics.ports import Labels, MetricsRecorder

logger = logging.getLogger(__name__)

#: One accumulated series: a metric name and its labels, frozen so it can key
#: a dict. Sorted, so two call sites passing the same labels in a different
#: order accumulate together rather than becoming two series.
_Series = tuple[str, tuple[tuple[str, str], ...]]


def _key(name: str, labels: Labels | None) -> _Series:
    return name, tuple(sorted((labels or {}).items()))


@dataclass(slots=True)
class AggregatingMetrics:
    """Sums counters until `flush`, and passes observations through.

    Wraps another recorder rather than writing anything itself, so the
    decision "how is a measurement emitted" stays in one place
    (`LoggingMetrics` today, an exporter tomorrow) and the decision "how often"
    lives here.

    **Not thread-safe, and it does not need to be.** Every caller is on the
    one asyncio event loop this process runs, and `increment` is a dictionary
    update with no await in it — so there is no interleaving point. A future
    threaded worker would need a lock, and would also need a different sink.
    """

    sink: MetricsRecorder
    _counters: dict[_Series, int] = field(default_factory=dict)

    def increment(self, name: str, *, labels: Labels | None = None, by: int = 1) -> None:
        """Accumulates. Emits nothing until `flush`.

        `by=0` is recorded rather than skipped, and that is deliberate: a
        series that exists with a value of zero is different from a series
        that does not exist, and the difference is exactly "the job ran and
        found nothing" versus "the job did not run". A retention pass that
        deleted nothing must still be visible.
        """
        key = _key(name, labels)
        self._counters[key] = self._counters.get(key, 0) + by

    def observe(self, name: str, value: float, *, labels: Labels | None = None) -> None:
        """Straight through to the sink — see this module's docstring on why
        an observation is not aggregated."""
        self.sink.observe(name, value, labels=labels)

    def flush(self) -> int:
        """Emits one record per accumulated series and resets. Returns how
        many series were emitted.

        **Resets rather than accumulating forever**, so each record is the
        delta for the interval and a log pipeline can `sum` over time without
        double counting. The alternative — a monotonic total, which is what a
        Prometheus counter is — needs a scrape rather than a push, and a
        pushed monotonic value is the one shape that silently breaks when a
        process restarts.

        Never raises: it runs from a scheduled task, and `MetricsRecorder`'s
        contract is that a metric never changes behaviour.
        """
        if not self._counters:
            return 0

        drained = self._counters
        self._counters = {}

        for (name, labels), total in drained.items():
            self.sink.increment(name, labels=dict(labels), by=total)
        return len(drained)

    def pending(self) -> Mapping[_Series, int]:
        """What has accumulated and not yet been flushed.

        For a test, and for the flush task's log line. Not a public metric —
        a gauge over this would be a gauge over the metrics system, which is
        the recursion `MetricsRecorder` declines to have.
        """
        return dict(self._counters)


__all__ = ["AggregatingMetrics"]
