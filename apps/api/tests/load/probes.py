"""What the machine was doing while the numbers were taken — §5, §30.

Throughput without saturation evidence is unfalsifiable: a number that does
not say whether the CPU was idle or the database pool was empty cannot tell
anybody what to provision. These are the cheapest honest readings.
"""

import asyncio
import contextlib
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One reading of the shared resources, taken beside a load run."""

    db_total: int
    db_active: int
    db_idle_in_transaction: int
    db_waiting: int
    redis_clients: int
    redis_ops_per_s: float
    redis_used_memory_mb: float
    api_cpu_percent: float
    api_rss_mb: float
    harness_cpu_percent: float = 0.0
    """The load generator's own CPU — A64-028.5A §11.

    Reported beside the API's so a reader can tell which side of the
    measurement ran out of room. A result taken while the generator is
    pinned is not a reading of the server, and A64-028.5 published a
    scaling efficiency of 0.52 that turned out to be exactly that."""
    at: float = 0.0
    """`perf_counter` when this reading was taken, so a peak can be
    attributed to the level that caused it — A64-028.5A §21. Without it the
    only honest thing to report was the peak over the whole ladder, which
    made a per-connection memory figure impossible to derive."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "db_connections": self.db_total,
            "db_active": self.db_active,
            "db_idle_in_txn": self.db_idle_in_transaction,
            "db_waiting": self.db_waiting,
            "redis_clients": self.redis_clients,
            "redis_ops_per_s": round(self.redis_ops_per_s, 1),
            "redis_memory_mb": round(self.redis_used_memory_mb, 1),
            "api_cpu_percent": round(self.api_cpu_percent, 1),
            "api_rss_mb": round(self.api_rss_mb, 1),
        }


async def observe(engine: AsyncEngine, redis: Redis, *, process_match: str) -> Snapshot:
    """Reads PostgreSQL, Redis and the API processes at one instant."""
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT count(*) AS total, "
                        "count(*) FILTER (WHERE state = 'active') AS active, "
                        "count(*) FILTER (WHERE state = 'idle in transaction') AS idle_txn, "
                        "count(*) FILTER (WHERE wait_event_type = 'Lock') AS waiting "
                        "FROM pg_stat_activity WHERE datname = current_database()"
                    )
                )
            )
            .mappings()
            .one()
        )

    info = await redis.info()
    cpu, rss = _process_totals(process_match)
    harness_cpu = _own_cpu()

    return Snapshot(
        db_total=row["total"],
        db_active=row["active"],
        db_idle_in_transaction=row["idle_txn"],
        db_waiting=row["waiting"],
        redis_clients=int(info.get("connected_clients", 0)),
        redis_ops_per_s=float(info.get("instantaneous_ops_per_sec", 0)),
        redis_used_memory_mb=float(info.get("used_memory", 0)) / 1024 / 1024,
        api_cpu_percent=cpu,
        api_rss_mb=rss,
        harness_cpu_percent=harness_cpu,
        at=time.perf_counter(),
    )


def _own_cpu() -> float:
    """This process's CPU, as `ps` reports it — §11.

    Deliberately the *generator's* own figure and nothing else's: the
    question it answers is whether a number was limited by the thing being
    measured or by the thing doing the measuring.
    """
    try:
        output = subprocess.run(  # noqa: S603
            ["/bin/ps", "-o", "pcpu=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return float("nan")
    try:
        return float(output.strip())
    except ValueError:
        return float("nan")


def _process_totals(match: str) -> tuple[float, float]:
    """Summed CPU% and RSS of the API processes.

    `ps` rather than `psutil`, which is not a dependency and would be one
    added for a measurement rather than for the product.
    """
    try:
        output = subprocess.run(  # noqa: S603
            ["/bin/ps", "-Ao", "pid,pcpu,rss,command"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return (float("nan"), float("nan"))

    # The harness runs `python -m tests.load ...`, and its own argv can
    # contain the string it is matching on. A probe that measured itself
    # would report a busy API on an idle machine, which is the most
    # misleading answer available.
    mine = {os.getpid(), os.getppid()}

    cpu = rss = 0.0
    for line in output.splitlines()[1:]:
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        pid, percent, resident, command = parts
        if match not in command or "/bin/ps" in command:
            continue
        try:
            if int(pid) in mine:
                continue
            cpu += float(percent)
            rss += float(resident) / 1024
        except ValueError:
            continue
    return (cpu, rss)


async def event_loop_lag(samples: int = 40, interval_s: float = 0.05) -> dict[str, float]:
    """How late a zero-length sleep comes back — §30.

    The cheapest measure of whether something is blocking the loop. Measured
    **in this process**, so it says what the *client* loop is doing; the
    API's own lag needs the same probe inside the API, which is
    instrumentation A64-028.6 owns. Stated rather than implied.
    """
    lags: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        await asyncio.sleep(interval_s)
        lags.append((time.perf_counter() - started - interval_s) * 1000)
    lags.sort()
    return {
        "lag_p50_ms": round(lags[len(lags) // 2], 2),
        "lag_p95_ms": round(lags[max(0, round(0.95 * len(lags)) - 1)], 2),
        "lag_max_ms": round(lags[-1], 2),
    }


async def outbox_health(engine: AsyncEngine) -> dict[str, Any]:
    """The regression A64-028.4 found, watched during load — §58.

    A relay that has died reports nothing: the symptom is a backlog whose
    `attempt_count` climbs and whose oldest entry stops moving. Both are
    read here so a load run cannot pass while the platform quietly stops
    delivering.
    """
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE published_at IS NULL) AS pending, "
                        "count(*) FILTER (WHERE published_at IS NULL "
                        "  AND attempt_count >= 5) AS exhausted, "
                        "max(attempt_count) FILTER (WHERE published_at IS NULL) AS max_attempts, "
                        "min(occurred_at) FILTER (WHERE published_at IS NULL) AS oldest "
                        "FROM platform.outbox"
                    )
                )
            )
            .mappings()
            .one()
        )
    return {
        "outbox_pending": row["pending"],
        "outbox_exhausted": row["exhausted"],
        "outbox_max_attempts": row["max_attempts"],
        "outbox_oldest_pending": str(row["oldest"]) if row["oldest"] else None,
    }


async def durable_invariants(engine: AsyncEngine) -> dict[str, int]:
    """The invariants that must hold however fast it went — §46.

    Fast wrong software is still wrong.
    """
    async with engine.connect() as connection:
        double_matched = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT player_id FROM ("
                    "    SELECT light_player_id AS player_id FROM game.match WHERE status='active'"
                    "    UNION ALL"
                    "    SELECT dark_player_id FROM game.match WHERE status='active'"
                    "  ) seats GROUP BY player_id HAVING count(*) > 1"
                    ") doubled"
                )
            )
        ).scalar_one()
        duplicate_plies = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT match_id, ply_number FROM game.move"
                    "  GROUP BY match_id, ply_number HAVING count(*) > 1"
                    ") duplicated"
                )
            )
        ).scalar_one()
        orphan_moves = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM game.move m "
                    "LEFT JOIN game.match x ON x.id = m.match_id WHERE x.id IS NULL"
                )
            )
        ).scalar_one()
        duplicate_ratings = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT match_id, player_id FROM rating.rating_adjustment"
                    "  GROUP BY match_id, player_id HAVING count(*) > 1"
                    ") duplicated"
                )
            )
        ).scalar_one()

    return {
        "players_in_two_active_matches": double_matched,
        "duplicate_plies": duplicate_plies,
        "orphan_moves": orphan_moves,
        "duplicate_rating_adjustments": duplicate_ratings,
    }


class Sampler:
    """Reads the shared resources **while** the load runs — §5.

    Taking one snapshot after a run measures an idle machine and reports it
    as the cost of the workload, which is worse than reporting nothing: the
    first version of this harness did exactly that and said the CPU was 36%
    busy at the moment nothing was happening.
    """

    def __init__(self, engine: AsyncEngine, redis: Redis, *, process_match: str) -> None:
        self._engine = engine
        self._redis = redis
        self._match = process_match
        self._samples: list[Snapshot] = []
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "Sampler":
        self._samples.clear()
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            # A failed reading must not stop the load it is observing.
            with contextlib.suppress(Exception):
                self._samples.append(
                    await observe(self._engine, self._redis, process_match=self._match)
                )
            await asyncio.sleep(0.5)

    def trend_between(self, start: float, end: float) -> dict[str, Any]:
        """First quarter against last quarter — A64-028.5A §29.

        A peak cannot tell a leak from a busy minute. What distinguishes
        them is direction: memory that rises and stays up across a long
        run is a leak, memory that rises and returns is the allocator
        doing its job. Quarters rather than endpoints, because a single
        first and last reading is one garbage collection away from saying
        anything at all.
        """
        window = [s for s in self._samples if start <= s.at <= end]
        if len(window) < 8:
            return {}
        quarter = max(1, len(window) // 4)
        opening = window[:quarter]
        closing = window[-quarter:]
        first = sum(s.api_rss_mb for s in opening) / len(opening)
        last = sum(s.api_rss_mb for s in closing) / len(closing)
        return {
            "rss_first_quarter_mb": round(first, 1),
            "rss_last_quarter_mb": round(last, 1),
            "rss_growth_mb": round(last - first, 1),
            "rss_growth_percent": round((last - first) / first * 100, 1) if first else None,
            "trend_readings": len(window),
        }

    def peak_between(self, start: float, end: float) -> dict[str, Any]:
        """The peak over one level's own window.

        Falls back to the whole run when the window caught no reading —
        a level shorter than the sampling interval is a real case, and a
        missing figure is better reported as the run's than as absent.
        """
        window = [s for s in self._samples if start <= s.at <= end]
        return self._peak_of(window or self._samples)

    def peak(self) -> dict[str, Any]:
        """The worst reading of each resource, which is what saturation is
        about — an average CPU hides the second it spent pinned."""
        return self._peak_of(self._samples)

    @staticmethod
    def _peak_of(samples: list[Snapshot]) -> dict[str, Any]:
        if not samples:
            return {}
        return {
            "db_connections_peak": max(s.db_total for s in samples),
            "db_active_peak": max(s.db_active for s in samples),
            "db_waiting_peak": max(s.db_waiting for s in samples),
            "redis_clients_peak": max(s.redis_clients for s in samples),
            "redis_ops_peak": round(max(s.redis_ops_per_s for s in samples), 1),
            "api_cpu_peak": round(max(s.api_cpu_percent for s in samples), 1),
            "api_rss_peak_mb": round(max(s.api_rss_mb for s in samples), 1),
            "harness_cpu_peak": round(max(s.harness_cpu_percent for s in samples), 1),
            "readings": len(samples),
        }
