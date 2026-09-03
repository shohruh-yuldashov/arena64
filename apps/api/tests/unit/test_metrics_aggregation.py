"""`AggregatingMetrics` and its flush schedule — A64-015.6 §6.

The rule this file is evidence for, and the arithmetic behind it:

    increment   accumulated in memory, one record per series per flush
    observe     straight through, one record per measurement

A counter summed over an interval loses nothing — the sum *is* the counter.
An observation summed over an interval loses the distribution, which is the
only thing an observation is for and is exactly what A64-015.5 §7's
deadline-tuning evidence reads. So the asymmetry is not a compromise, and
these tests hold both halves of it.

The real accumulator, the real flush task and the real `MetricsRecorder`
contract run here. What is substituted is the sink, so what was emitted is
countable.
"""

import pytest

from app.platform.metrics import (
    METRICS_FLUSH_TASK,
    AggregatingMetrics,
    MetricsFlushTask,
    flush_request,
    process_metrics,
)
from tests.fakes.metrics import RecordingMetrics


@pytest.fixture
def sink() -> RecordingMetrics:
    return RecordingMetrics()


@pytest.fixture
def metrics(sink: RecordingMetrics) -> AggregatingMetrics:
    return AggregatingMetrics(sink=sink)


class TestCountersAreHeldUntilFlush:
    def test_an_increment_emits_nothing(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        """The whole point. A naive recorder on the pairing scan is ~1.2
        million log records a day on a platform with no players on it."""
        metrics.increment("matchmaking.pairing_scans_total", labels={"outcome": "idle"})

        assert sink.recorded == []

    def test_a_flush_emits_one_record_per_series(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        for _ in range(840):
            metrics.increment("matchmaking.pairing_scans_total", labels={"outcome": "idle"})

        assert metrics.flush() == 1
        assert len(sink.recorded) == 1

    def test_the_emitted_value_is_the_sum(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        """Lossless: a rate query over one record of 840 and over 840 records
        of one returns the same number."""
        for _ in range(840):
            metrics.increment("matchmaking.pairing_scans_total", labels={"outcome": "idle"})
        metrics.flush()

        assert sink.recorded[0].value == 840.0

    def test_the_labels_survive_the_round_trip(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        metrics.increment("matchmaking.pairing_scans_total", labels={"outcome": "paired"})
        metrics.flush()

        assert sink.recorded[0].labels == {"outcome": "paired"}

    def test_a_custom_step_accumulates(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        metrics.increment("matchmaking.pairing_candidates_total", by=12)
        metrics.increment("matchmaking.pairing_candidates_total", by=30)
        metrics.flush()

        assert sink.recorded[0].value == 42.0

    def test_two_labels_of_one_metric_stay_two_series(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        """Aggregation must not aggregate away the dimension somebody wanted
        — "how often did a scan pair" and "how often was it idle" are
        different questions about the same counter."""
        metrics.increment("matchmaking.pairing_scans_total", labels={"outcome": "idle"}, by=800)
        metrics.increment("matchmaking.pairing_scans_total", labels={"outcome": "paired"}, by=40)

        assert metrics.flush() == 2
        assert sink.counts("matchmaking.pairing_scans_total") == {"idle": 800.0, "paired": 40.0}

    def test_the_same_labels_in_a_different_order_are_one_series(
        self, metrics: AggregatingMetrics
    ) -> None:
        """The key is sorted, so two call sites spelling their labels in
        different orders accumulate together rather than becoming two."""
        metrics.increment("thing", labels={"a": "1", "b": "2"})
        metrics.increment("thing", labels={"b": "2", "a": "1"})

        assert metrics.flush() == 1

    def test_an_unlabelled_metric_accumulates(self, metrics: AggregatingMetrics) -> None:
        metrics.increment("matchmaking.pairing_candidates_total")
        metrics.increment("matchmaking.pairing_candidates_total")

        assert metrics.pending() == {("matchmaking.pairing_candidates_total", ()): 2}

    def test_a_zero_step_is_recorded_rather_than_skipped(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        """A series that exists with a value of zero says "the job ran and
        found nothing"; an absent series says "the job did not run", and
        telling those apart is a retention metric's whole value."""
        metrics.increment("matchmaking.retention_deletions_total", by=0)

        assert metrics.flush() == 1
        assert sink.recorded[0].value == 0.0


class TestFlushResets:
    def test_a_second_flush_emits_nothing(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        """Each record is the delta for its interval, so a log pipeline can
        sum over time without double counting."""
        metrics.increment("thing")
        metrics.flush()

        assert metrics.flush() == 0
        assert len(sink.recorded) == 1

    def test_counting_resumes_after_a_flush(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        metrics.increment("thing", by=3)
        metrics.flush()
        metrics.increment("thing", by=4)
        metrics.flush()

        assert [record.value for record in sink.recorded] == [3.0, 4.0]

    def test_an_empty_accumulator_flushes_cleanly(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        assert metrics.flush() == 0
        assert sink.recorded == []


class TestObservationsKeepTheirFidelity:
    def test_an_observation_reaches_the_sink_immediately(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        """No flush. §2's deadline evidence is read from a live histogram,
        and a measurement held for a minute is a measurement that is not
        there when somebody looks."""
        metrics.observe("game.match_answer_latency_seconds", 4.2)

        assert sink.observations("game.match_answer_latency_seconds") == [4.2]

    def test_every_measurement_is_kept_separately(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        """Summing observations would lose the distribution — and a `p99` and
        the shape of the tail are the only things §2 can tune from."""
        for value in (1.0, 2.0, 30.0):
            metrics.observe("game.match_answer_latency_seconds", value)

        assert sink.observations("game.match_answer_latency_seconds") == [1.0, 2.0, 30.0]

    def test_observations_are_not_held_by_a_flush(self, metrics: AggregatingMetrics) -> None:
        metrics.observe("game.match_answer_latency_seconds", 4.2)

        assert metrics.flush() == 0

    def test_an_observations_labels_survive(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        metrics.observe("game.match_answer_latency_seconds", 4.2, labels={"answer": "accepted"})

        assert sink.recorded[0].labels == {"answer": "accepted"}


class TestTheFlushTask:
    @pytest.mark.asyncio
    async def test_running_it_drains_the_accumulator(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        metrics.increment("thing", by=7)

        await MetricsFlushTask(metrics=metrics).run({})

        assert sink.recorded[0].value == 7.0

    @pytest.mark.asyncio
    async def test_it_holds_the_accumulator_rather_than_a_factory(
        self, metrics: AggregatingMetrics, sink: RecordingMetrics
    ) -> None:
        """The opposite of every other task on this platform, and right for
        the opposite reason: the counters *are* process state, so a per-run
        accumulator would flush an empty dictionary forever."""
        task = MetricsFlushTask(metrics=metrics)
        metrics.increment("thing")
        await task.run({})
        metrics.increment("thing")
        await task.run({})

        assert len(sink.recorded) == 2

    @pytest.mark.asyncio
    async def test_a_broken_sink_does_not_stop_the_schedule(
        self, metrics: AggregatingMetrics
    ) -> None:
        """`MetricsRecorder`'s contract is that a metric never changes
        behaviour. A flush that propagated would stop the schedule that
        drains it, turning a broken sink into permanently lost counters
        rather than one lost interval."""

        class _Broken:
            def increment(self, name: str, **kwargs: object) -> None:
                raise RuntimeError("the sink is on fire")

            def observe(self, name: str, value: float, **kwargs: object) -> None:
                raise RuntimeError("the sink is on fire")

        broken = AggregatingMetrics(sink=_Broken())
        broken.increment("thing")

        await MetricsFlushTask(metrics=broken).run({})

    def test_the_task_answers_to_the_dispatched_name(self, metrics: AggregatingMetrics) -> None:
        assert MetricsFlushTask(metrics=metrics).name == METRICS_FLUSH_TASK

    def test_the_request_carries_no_payload(self) -> None:
        """There is nothing to parameterise, and a request carrying the
        accumulator would be a request nobody could put on a broker."""
        assert flush_request().name == METRICS_FLUSH_TASK
        assert flush_request().payload == {}


class TestOneAccumulatorPerProcess:
    """§10: the request path and the worker path must count into the same
    object, because `MetricsFlushTask` drains exactly one."""

    def test_the_accessor_hands_out_the_same_instance(self) -> None:
        assert process_metrics() is process_metrics()

    def test_a_counter_taken_through_one_reference_is_visible_through_the_other(
        self,
    ) -> None:
        """The defect this closes: two recorders meant counters lived in
        whichever instance took them, and half of them were never drained."""
        process_metrics.cache_clear()
        try:
            process_metrics().increment("thing", by=5)

            assert process_metrics().pending() == {("thing", ()): 5}
        finally:
            process_metrics.cache_clear()

    def test_the_composition_root_and_the_request_path_share_it(self) -> None:
        """Asserted through the two accessors that actually exist rather than
        by reading either one's source."""
        from app.app_factory import _metrics
        from app.modules.matchmaking.presentation.dependencies import get_metrics

        assert _metrics() is get_metrics()
