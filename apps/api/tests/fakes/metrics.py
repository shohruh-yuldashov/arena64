"""A `MetricsRecorder` that keeps what it was given — A64-015.5.

`NullMetrics` ships and records nothing, which is right for a service whose
subject is something else. This one is for the tests that *are* about the
measurement: §7 requires answer latency to be instrumented before the
thirty-second deadline is tuned, and §9 requires the reconciler's outcomes to
be countable with bounded labels. Neither is assertable against a recorder
that discards.

It keeps every call rather than aggregating, so a test can assert the label
as well as the count — which is the half §9 is actually about.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Measurement:
    """One `increment` or `observe`, as it was called."""

    name: str
    value: float
    labels: Mapping[str, str]


@dataclass(slots=True)
class RecordingMetrics:
    """Every measurement, in order."""

    recorded: list[Measurement] = field(default_factory=list)

    def increment(self, name: str, *, labels: Mapping[str, str] | None = None, by: int = 1) -> None:
        self.recorded.append(Measurement(name=name, value=float(by), labels=dict(labels or {})))

    def observe(self, name: str, value: float, *, labels: Mapping[str, str] | None = None) -> None:
        self.recorded.append(Measurement(name=name, value=value, labels=dict(labels or {})))

    def counts(self, name: str) -> dict[str, float]:
        """Total by the value of the single label `name` carries.

        Every metric this platform emits has exactly one label, so reducing
        to a `dict` keyed on its value is lossless — and it is the shape a
        test asserts in, because "how many were `requeued`" is the question
        rather than "what was the third call".
        """
        totals: dict[str, float] = {}
        for measurement in self.recorded:
            if measurement.name != name:
                continue
            for value in measurement.labels.values():
                totals[value] = totals.get(value, 0.0) + measurement.value
        return totals

    def observations(self, name: str, *, label: str | None = None) -> list[float]:
        """Every value observed for `name`, optionally filtered by label."""
        return [
            measurement.value
            for measurement in self.recorded
            if measurement.name == name and (label is None or label in measurement.labels.values())
        ]

    def label_values(self) -> set[str]:
        """Every label value ever emitted.

        The set §9's cardinality rule is about: a test asserts it contains
        no identifier, which is what "bounded labels" means in practice.
        """
        return {value for measurement in self.recorded for value in measurement.labels.values()}


__all__ = ["Measurement", "RecordingMetrics"]
