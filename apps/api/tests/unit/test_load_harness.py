"""The load harness's own arithmetic — A64-028.5A §41.

A benchmark is measuring instrumentation, and unmeasured instrumentation
publishes numbers nobody can check. Two of this harness's defects reached a
report before these existed: a percentile taken over failures, which read
the client's timeout as the server's latency, and a refusal counted as
throughput, which made a saturated service answering `429` look faster than
a healthy one.

Only the pure parts are here. What the harness does against a server is
proven by using it; what it does with the numbers afterwards is proven
here, because a wrong p95 is invisible in a table.
"""

from tests.load.harness import Result, Sample


def _result(*samples: Sample, duration_s: float = 1.0) -> Result:
    return Result(scenario="t", concurrency=1, duration_s=duration_s, samples=list(samples))


class TestWhatCountsAsSuccess:
    def test_a_2xx_is_a_success(self) -> None:
        assert Sample(elapsed_s=0.1, status=200).ok

    def test_a_frame_without_a_status_is_a_success(self) -> None:
        """WebSocket samples carry no status; absent must not read as bad."""
        assert Sample(elapsed_s=0.1).ok

    def test_an_error_is_never_a_success_whatever_the_status(self) -> None:
        assert not Sample(elapsed_s=0.1, status=200, error="Timeout").ok

    def test_a_refusal_is_neither_a_success_nor_a_failure(self) -> None:
        refusal = Sample(elapsed_s=0.01, status=429)
        assert not refusal.ok
        assert refusal.expected_refusal

    def test_a_500_is_a_failure_and_not_a_refusal(self) -> None:
        """The distinction the whole classification exists for: a limiter
        holding is the platform working, a `500` is the platform broken."""
        crash = Sample(elapsed_s=0.01, status=500)
        assert not crash.ok
        assert not crash.expected_refusal


class TestThroughput:
    def test_only_successes_count(self) -> None:
        """A service answering `429` instantly must not out-score a healthy
        one — the failure mode that made refusals a separate column."""
        result = _result(
            Sample(elapsed_s=0.1, status=200),
            *[Sample(elapsed_s=0.001, status=429)] * 99,
            duration_s=1.0,
        )

        assert result.throughput == 1.0

    def test_a_zero_length_run_reports_no_throughput_rather_than_dividing(self) -> None:
        assert _result(Sample(elapsed_s=0.1, status=200), duration_s=0.0).throughput == 0.0


class TestPercentiles:
    def test_they_are_taken_over_successes_only(self) -> None:
        """A timeout's elapsed time is the timeout. Letting it in makes p99
        a reading of the client's patience."""
        result = _result(
            *[Sample(elapsed_s=0.01, status=200)] * 99,
            Sample(elapsed_s=30.0, error="Timeout"),
        )

        assert result.percentile(0.99) == 10.0

    def test_nearest_rank_picks_a_value_that_was_observed(self) -> None:
        result = _result(*[Sample(elapsed_s=n / 100, status=200) for n in range(1, 101)])

        assert result.percentile(0.50) == 500.0
        assert result.percentile(0.95) == 950.0

    def test_a_run_with_no_successes_reports_nan_rather_than_zero(self) -> None:
        """Zero would read as instant. `nan` reads as absent, which is what
        it is."""
        result = _result(Sample(elapsed_s=0.5, error="ConnectionReset"))

        assert result.percentile(0.50) != result.percentile(0.50)


class TestRefusalsAreBrokenDownByStatus:
    """A wall of `429` and a run of `409` mean opposite things — §14.

    The first says the limiter held and the service was never asked for the
    work, so the throughput beside it is a reading of the limit. The second
    says sessions genuinely raced. One total conflates a protected service
    with a struggling one.
    """

    def test_each_refused_status_is_counted_separately(self) -> None:
        result = _result(
            *[Sample(elapsed_s=0.01, status=429)] * 3,
            Sample(elapsed_s=0.01, status=409),
            Sample(elapsed_s=0.01, status=200),
        )

        assert result.refusals_by_status == {"429": 3, "409": 1}

    def test_failures_are_not_included(self) -> None:
        result = _result(Sample(elapsed_s=0.01, status=503))

        assert result.refusals_by_status == {}
