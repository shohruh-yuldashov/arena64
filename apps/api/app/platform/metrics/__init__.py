"""Counters and durations, as a port — A64-015.5.

The platform had no metrics surface before this task, and A64-015.5 needs
one for a reason that is not "metrics are good": §7 forbids tuning the
thirty-second acceptance deadline from intuition, and §9 requires the
reconciler's outcomes to be *countable* rather than merely logged. Both are
decisions that must be made from data the platform does not currently
produce.

    MetricsRecorder   what a service holds
    LoggingMetrics    the implementation that ships
    NullMetrics       the implementation a test holds when metrics are not
                      the subject

## Why `app/platform` and not a module

Every module will eventually want to count something, so a recorder owned
by whichever module needed one first would make every other module import
it. Nothing here imports `app.modules`, and `.importlinter` fails if that
changes — the same rule the outbox and the task dispatcher follow.

## Why the shipped implementation writes log lines

There is no metrics backend in this deployment: no Prometheus, no StatsD, no
OpenTelemetry collector, and adding one is outside a task's authority
(CLAUDE.md §11 — do not install dependencies without instruction). What
exists is a structured JSON log pipeline that every service already writes
to, and a counter emitted there is queryable today.

**This is a seam, not a stub.** Everything upstream of it is real: the
measurement is taken at the right instant, from the injected clock, with
bounded labels. What is missing is only the exporter, and the day one exists
it is a second implementation of this protocol wired at the composition root
— nothing above changes. That is the same argument `NotificationSink`
records, and it has held through two tasks.

## Labels are bounded, and that is a rule rather than a convention

A64-015.5 §9 is explicit: no player ids, no match ids, no pairing ids. The
reason is not privacy — the log pipeline already holds those — it is
**cardinality**: a counter labelled by match id is one time series per
match, which is a metrics backend that falls over on the day the platform
succeeds.

So `Labels` is `Mapping[str, str]` and every call site passes members of a
closed enumeration. `LoggingMetrics` does not enforce that (it cannot see
the difference), and a test does: see
`tests/unit/test_matchmaking_metrics.py`, which asserts every label value
this platform emits comes from a `StrEnum`.
"""

from app.platform.metrics.ports import (
    Labels,
    LoggingMetrics,
    MetricsRecorder,
    NullMetrics,
)

__all__ = ["Labels", "LoggingMetrics", "MetricsRecorder", "NullMetrics"]
