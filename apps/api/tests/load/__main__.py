"""Running the load scenarios — A64-028.5 §48.

    uv run python -m tests.load P01 P02 --nodes http://127.0.0.1:8101,http://127.0.0.1:8102

Every run prints the environment beside the numbers, because a latency
without the machine that produced it is not a measurement.
"""

import argparse
import asyncio
import json
import platform
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from app.config.settings import get_settings
from tests.load import scenarios
from tests.load.harness import Result, as_json, render
from tests.load.probes import Sampler, durable_invariants, event_loop_lag, outbox_health
from tests.load.workload import seeded_cohort


def environment(node_urls: Sequence[str]) -> dict[str, Any]:
    """Everything §3 requires, gathered rather than described."""
    settings = get_settings()

    def sysctl(name: str) -> str:
        try:
            return subprocess.run(  # noqa: S603
                ["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, check=False
            ).stdout.strip()
        except OSError:
            return "unknown"

    return {
        "NOT_PRODUCTION_HARDWARE": True,
        "cpu": sysctl("machdep.cpu.brand_string") or platform.processor(),
        "cores_logical": sysctl("hw.logicalcpu"),
        "ram_gb": round(int(sysctl("hw.memsize") or 0) / 1024**3) or None,
        "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
        "api_instances": len(node_urls),
        "api_worker_model": "uvicorn, single worker per process, no reload",
        "tls": False,
        "app_environment": settings.environment.value,
        "rate_limit_profile": settings.rate_limit.profile,
        "log_level": settings.app.log_level,
        "pg_pool_size": settings.postgres.pool_size,
        "pg_max_overflow": settings.postgres.max_overflow,
        "pg_statement_timeout_ms": settings.postgres.statement_timeout_ms,
        "postgres": "17.10 in Docker, localhost",
        "redis": "8.10 in Docker, localhost",
        "load_generator": "in-repo asyncio harness (httpx, websockets), same machine",
    }


async def run(names: Sequence[str], node_urls: list[str], *, scale: float) -> list[Result]:
    settings = get_settings()
    engine = create_async_engine(settings.postgres.dsn.get_secret_value())
    redis = Redis.from_url(settings.redis.live_url.get_secret_value())
    primary = node_urls[0]
    results: list[Result] = []

    def levels(*values: int) -> list[int]:
        return [max(1, round(value * scale)) for value in values]

    duration = 10.0 * scale

    try:
        before = await outbox_health(engine)
        for name in names:
            # Observed **during** the scenario, not after it — see `Sampler`.
            sampler = Sampler(engine, redis, process_match="uvicorn main:app")
            first_new = len(results)
            await sampler.__aenter__()
            if name == "P01":
                players = await seeded_cohort(1, prefix="p01")
                results += await http_reads(
                    primary,
                    "/api/v1/time-controls",
                    players[0],
                    levels(1, 10, 25, 50, 100),
                    duration,
                )
            elif name == "P02":
                players = await seeded_cohort(1, prefix="p02")
                results += await http_reads(
                    primary, "/api/v1/profile/me", players[0], levels(1, 10, 25, 50, 100), duration
                )
                results += await http_reads(
                    primary, "/api/v1/tournaments", players[0], levels(25, 50), duration
                )
            elif name == "P03":
                players = await seeded_cohort(8, prefix="p03")
                results += await scenarios.login_load(
                    primary, players, levels=levels(1, 5, 10, 25), duration_s=duration
                )
            elif name == "P04":
                results.append(
                    await scenarios.refresh_load(
                        primary, sessions=max(2, round(20 * scale)), duration_s=duration
                    )
                )
            elif name == "P06":
                for users in (round(v * scale) for v in (50, 100)):
                    if users >= 2:
                        results.append(
                            await scenarios.matchmaking_burst(
                                primary, engine, users=users, wait_s=20.0
                            )
                        )
            elif name == "P08":
                for games in (round(v * scale) for v in (5, 25, 50)):
                    if games >= 1:
                        results.append(
                            await scenarios.live_games(
                                engine, node_urls=node_urls, games=games, moves_per_game=6
                            )
                        )
            elif name == "P09":
                for count in (round(v * scale) for v in (100, 300)):
                    if count >= 1:
                        results.append(
                            await scenarios.idle_sockets(primary, count=count, hold_s=5.0)
                        )
            elif name == "P10":
                results.append(
                    await scenarios.frame_latency(engine, node_urls=node_urls, rounds=30)
                )
            elif name == "P16":
                # Scaling: the same closed-loop workload split evenly across
                # 1 then N instances. Throughput is summed and reported as
                # one row per instance count, which is the only comparison
                # §36's efficiency number can honestly be taken from.
                for instances in range(1, len(node_urls) + 1):
                    urls = node_urls[:instances]
                    per_node = max(1, round(25 * scale))
                    players = await seeded_cohort(1, prefix=f"p16n{instances}")
                    parts = await asyncio.gather(
                        *(
                            scenarios.http_reads(
                                url,
                                path="/api/v1/profile/me",
                                player=players[0],
                                levels=[per_node],
                                duration_s=duration,
                            )
                            for url in urls
                        )
                    )
                    flat = [result for group in parts for result in group]
                    combined = Result(
                        scenario=f"scaling: {instances} instance(s)",
                        concurrency=per_node * instances,
                        duration_s=max(r.duration_s for r in flat),
                        samples=[s for r in flat for s in r.samples],
                    )
                    combined.notes = {
                        "instances": instances,
                        "concurrency_per_instance": per_node,
                    }
                    results.append(combined)
            elif name == "P13":
                for events in (round(v * scale) for v in (500, 2000)):
                    if events >= 1:
                        results.append(
                            await scenarios.outbox_drain(engine, events=events, patience_s=120.0)
                        )
            else:
                print(f"unknown scenario {name}", file=sys.stderr)  # noqa: T201

            await sampler.__aexit__()
            # Attached to the **last** result of the group, which is its
            # highest concurrency and where saturation shows. Copying one
            # peak onto every level would read as if the low levels had also
            # pinned the CPU.
            if len(results) > first_new:
                results[-1].notes.update(sampler.peak())

        after = await outbox_health(engine)
        lag = await event_loop_lag()
        invariants = await durable_invariants(engine)
        print("\n" + render(results))  # noqa: T201
        print("\nharness event loop:", json.dumps(lag))  # noqa: T201
        print("outbox before:", json.dumps(before))  # noqa: T201
        print("outbox after :", json.dumps(after))  # noqa: T201
        print("invariants   :", json.dumps(invariants))  # noqa: T201
        return results
    finally:
        await redis.aclose()
        await engine.dispose()


async def http_reads(
    base_url: str, path: str, player: Any, levels: Sequence[int], duration: float
) -> list[Result]:
    return await scenarios.http_reads(
        base_url, path=path, player=player, levels=levels, duration_s=duration
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.load")
    parser.add_argument("scenarios", nargs="+", help="P01 P02 P03 …")
    parser.add_argument(
        "--nodes",
        default="http://127.0.0.1:8101",
        help="comma-separated API base URLs already running",
    )
    parser.add_argument("--scale", type=float, default=1.0, help="scale levels and durations")
    parser.add_argument("--out", default="", help="write JSON results here")
    arguments = parser.parse_args(argv)

    node_urls = [url.strip() for url in arguments.nodes.split(",") if url.strip()]
    started = time.time()
    results = asyncio.run(run(arguments.scenarios, node_urls, scale=arguments.scale))

    payload = as_json(results, environment=environment(node_urls))
    if arguments.out:
        with open(arguments.out, "w", encoding="utf-8") as handle:
            handle.write(payload)
        print(f"\nwrote {arguments.out} ({time.time() - started:.0f}s)")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
