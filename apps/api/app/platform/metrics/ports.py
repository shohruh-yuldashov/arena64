"""The metrics port and the two implementations that ship.

See `app/platform/metrics/__init__.py` for why this exists and why the
shipped recorder writes structured log lines.
"""

import logging
from collections.abc import Mapping
from contextlib import suppress
from typing import Protocol

logger = logging.getLogger(__name__)

#: A metric's dimensions.
#:
#: `str -> str`, and every value a call site passes is a member of a closed
#: `StrEnum`. See this package's docstring on why that is a rule: a label
#: whose domain is unbounded — a player id, a match id — is one time series
#: per value, which is a metrics backend that falls over exactly when the
#: platform succeeds.
Labels = Mapping[str, str]


class MetricsRecorder(Protocol):
    """What a service holds to count things.

    Two methods, because there are two shapes of question and they are
    answered by different backend primitives:

        increment   how many times did this happen        (counter)
        observe     how long did it take, how big was it  (histogram)

    Deliberately **not** a gauge. A gauge is a value read at scrape time,
    which needs the exporter to call *into* the application — the one shape
    that cannot be expressed as a log line, and therefore the one this seam
    would be lying about. Anything currently gauge-shaped on this platform
    (queue depth, outbox backlog) is already a query against a partial
    index, which is a better answer than a cached number.

    **Never raises.** CLAUDE.md §8.10 makes this non-negotiable for logging
    and the same reasoning applies with more force here: a metric that could
    fail a request would be an observability feature causing the outage it
    exists to reveal.
    """

    def increment(self, name: str, *, labels: Labels | None = None, by: int = 1) -> None:
        """Counts an occurrence.

        `name` is dotted and namespaced by owning context — `matchmaking.*`,
        `game.*` — exactly as `DomainEvent.event_type` is, so an operator
        filtering by producer filters on the prefix.
        """
        ...

    def observe(self, name: str, value: float, *, labels: Labels | None = None) -> None:
        """Records one measurement.

        Durations are **seconds as a float**, platform-wide. Milliseconds
        would be the other defensible choice and mixing the two is the
        failure that matters — a dashboard reading `p99 = 1200` cannot tell
        a slow second from a fast twenty minutes.
        """
        ...


class LoggingMetrics:
    """Emits each measurement as one structured log record.

    The implementation that ships. `INFO`, because a metric is a business
    observation rather than diagnostic detail, and one record per
    measurement rather than per batch — unlike a delivery log, a counter
    that was aggregated before emission is a counter that has lost the
    dimension somebody wanted.

    The record carries a fixed `metric` field so a log pipeline can route on
    it without parsing the message, and the labels are spread onto the
    record rather than nested, because a JSON log query filters on flat
    fields.
    """

    def __init__(self, level: int = logging.INFO) -> None:
        self._level = level

    def increment(self, name: str, *, labels: Labels | None = None, by: int = 1) -> None:
        self._emit("counter", name, float(by), labels)

    def observe(self, name: str, value: float, *, labels: Labels | None = None) -> None:
        self._emit("observation", name, value, labels)

    def _emit(self, kind: str, name: str, value: float, labels: Labels | None) -> None:
        # Never raises: a broken label mapping must not fail the business
        # operation that was being measured. The suppression is deliberate
        # and is the one place on this platform where swallowing everything
        # is correct — see `MetricsRecorder` on why, and CLAUDE.md §8.10 for
        # the rule it applies.
        with suppress(Exception):
            logger.log(
                self._level,
                "metric",
                extra={"metric": name, "metric_kind": kind, "value": value, **(labels or {})},
            )


class NullMetrics:
    """Records nothing.

    For a test whose subject is not the metric, and for a deployment that
    wants the seam wired and silent. A real class rather than `None`, so no
    service holds an optional recorder and no call site grows an `if` that
    would outlive the reason for it — the argument `NoRecentOpponents` made
    and `AlwaysEligible` still makes.
    """

    def increment(self, name: str, *, labels: Labels | None = None, by: int = 1) -> None:
        return None

    def observe(self, name: str, value: float, *, labels: Labels | None = None) -> None:
        return None


__all__ = ["Labels", "LoggingMetrics", "MetricsRecorder", "NullMetrics"]
