"""Measuring, and being honest about what was measured — A64-028.5 §5, §47.

## Why a harness and not k6

k6 is the better tool for HTTP alone and cannot do the two things this
platform's hot paths need: drive a WebSocket through the gateway's own
handshake (a one-time ticket from an authenticated HTTP call), and assert
durable invariants in PostgreSQL while the load is running (§46). A harness
that measured throughput and could not tell whether a game was corrupted
while it did so would answer the less important question.

`httpx` and `websockets` are already dependencies, so this adds none.

## What it refuses to do

**It never reports a mean latency alone** (§5), it never counts an aborted
request as a success (§66-L), and it separates errors the platform is
*supposed* to produce from the ones it is not (§47). A benchmark that
folds `429 Too Many Requests` into an error rate is measuring the rate
limiter and calling it a fault.
"""

import asyncio
import json
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Statuses the platform produces **on purpose** under load, and which are
#: therefore not failures of it — §47.
#:
#:   429  the rate limiter refusing, which is the limiter working
#:   409  a rotation conflict (A64-028.2) or a queue cooldown; the client
#:        retries and continues
EXPECTED_STATUSES: frozenset[int] = frozenset({409, 429})


@dataclass(frozen=True, slots=True)
class Sample:
    """One measured operation.

    `status` is `None` for a WebSocket frame or anything else without one;
    `error` is the exception type's name, never its message, because a
    message can carry a token or an email address.
    """

    elapsed_s: float
    status: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (self.status is None or self.status < 400)

    @property
    def expected_refusal(self) -> bool:
        return self.error is None and self.status in EXPECTED_STATUSES


@dataclass
class Result:
    """One scenario at one concurrency, with the numbers it is allowed to
    report."""

    scenario: str
    concurrency: int
    duration_s: float
    samples: list[Sample] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.samples)

    @property
    def successes(self) -> int:
        return sum(1 for sample in self.samples if sample.ok)

    @property
    def refusals(self) -> int:
        """Expected refusals — the limiter and deliberate conflicts."""
        return sum(1 for sample in self.samples if sample.expected_refusal)

    @property
    def failures(self) -> int:
        """Everything the platform was not supposed to do."""
        return self.total - self.successes - self.refusals

    @property
    def throughput(self) -> float:
        """Successful operations per second over the wall clock.

        Successes only: counting refusals as throughput would let a
        saturated service that answers `429` instantly look faster than a
        healthy one.
        """
        return self.successes / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.total if self.total else 0.0

    def percentile(self, fraction: float) -> float:
        """Nearest-rank, over **successful** samples, in milliseconds.

        Successful only, deliberately: a timeout's elapsed time is the
        timeout, and letting it into the distribution makes p99 a reading of
        the client's patience rather than the server's latency. Failures are
        counted separately and reported beside these.
        """
        latencies = sorted(s.elapsed_s for s in self.samples if s.ok)
        if not latencies:
            return float("nan")
        index = max(0, min(len(latencies) - 1, round(fraction * len(latencies)) - 1))
        return latencies[index] * 1000

    def summary(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "concurrency": self.concurrency,
            "duration_s": round(self.duration_s, 2),
            "operations": self.total,
            "throughput_per_s": round(self.throughput, 1),
            "p50_ms": round(self.percentile(0.50), 1),
            "p95_ms": round(self.percentile(0.95), 1),
            "p99_ms": round(self.percentile(0.99), 1),
            "max_ms": round(max((s.elapsed_s for s in self.samples if s.ok), default=0) * 1000, 1),
            "expected_refusals": self.refusals,
            "failures": self.failures,
            "failure_rate": round(self.failure_rate, 5),
            **self.notes,
        }


async def timed(operation: Callable[[], Awaitable[int | None]]) -> Sample:
    """Runs one operation and records how it went.

    `perf_counter`, not the wall clock: everything here runs on one machine
    and a monotonic clock cannot be moved by NTP mid-run (§17).
    """
    started = time.perf_counter()
    try:
        status = await operation()
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 — classifying the failure is the job
        return Sample(elapsed_s=time.perf_counter() - started, error=type(error).__name__)
    return Sample(elapsed_s=time.perf_counter() - started, status=status)


async def run_for(
    scenario: str,
    *,
    operation: Callable[[int], Awaitable[int | None]],
    concurrency: int,
    duration_s: float,
) -> Result:
    """`concurrency` workers looping `operation` for `duration_s`.

    Closed-loop, which is the right model for this platform: a player makes
    a move, waits for the answer, then makes another. An open-loop generator
    would queue requests a real client would never have sent and turn a
    saturated server's latency into an artefact of the generator.
    """
    deadline = time.perf_counter() + duration_s
    samples: list[Sample] = []
    started = time.perf_counter()

    async def worker(index: int) -> None:
        while time.perf_counter() < deadline:
            samples.append(await timed(lambda: operation(index)))

    await asyncio.gather(*(worker(index) for index in range(concurrency)))
    return Result(
        scenario=scenario,
        concurrency=concurrency,
        duration_s=time.perf_counter() - started,
        samples=samples,
    )


def render(results: Sequence[Result]) -> str:
    """A table, because a wall of JSON is not a result anybody reads."""
    header = (
        f"{'scenario':<28} {'conc':>5} {'ops/s':>8} {'p50':>7} {'p95':>8} "
        f"{'p99':>8} {'refused':>8} {'failed':>7}"
    )
    lines = [header, "-" * len(header)]
    for result in results:
        row = result.summary()
        lines.append(
            f"{row['scenario']:<28} {row['concurrency']:>5} {row['throughput_per_s']:>8.1f} "
            f"{row['p50_ms']:>7.1f} {row['p95_ms']:>8.1f} {row['p99_ms']:>8.1f} "
            f"{row['expected_refusals']:>8} {row['failures']:>7}"
        )
    return "\n".join(lines)


def as_json(results: Sequence[Result], *, environment: dict[str, Any]) -> str:
    """Machine-readable, for comparing one commit against another (§49).

    The environment travels with the numbers because a number without it is
    not a measurement — it is a rumour.
    """
    return json.dumps(
        {"environment": environment, "results": [r.summary() for r in results]},
        indent=2,
        sort_keys=True,
    )


def mean_ms(values: Sequence[float]) -> float:
    return statistics.fmean(values) * 1000 if values else float("nan")
