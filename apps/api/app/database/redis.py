"""Redis initialization — architecture.md AD-03: four role-separated
instances, never one shared client.

Each role gets its own connection pool so role separation is real at the
code level, not only at the settings level — reusing one pool object across
roles would silently reintroduce the coupling AD-03 exists to prevent, even
when the underlying URLs happen to point at the same instance in `local`.
"""

from dataclasses import dataclass

from redis.asyncio import Redis

from app.config.settings import RedisSettings


@dataclass(frozen=True, slots=True)
class RedisPools:
    """One client per role (architecture.md AD-03).

    `live`   — live match position and clocks
    `bus`    — pub/sub fan-out
    `broker` — Celery broker and task queues (unused until a worker entrypoint exists)
    `cache`  — response cache and read models
    """

    live: Redis
    bus: Redis
    broker: Redis
    cache: Redis

    async def aclose(self) -> None:
        for client in (self.live, self.bus, self.broker, self.cache):
            await client.aclose()

    async def ping_all(self) -> dict[str, bool]:
        """Used by the readiness check (app/api/v1/health.py) only — never
        on a hot path, so a per-role try/except here is the right place to
        absorb a single role's outage rather than fail the whole probe."""
        results: dict[str, bool] = {}
        for name, client in (
            ("live", self.live),
            ("bus", self.bus),
            ("broker", self.broker),
            ("cache", self.cache),
        ):
            try:
                results[name] = bool(await client.ping())
            except Exception:  # noqa: BLE001 — a readiness probe must not raise
                results[name] = False
        return results


def create_redis_pools(settings: RedisSettings) -> RedisPools:
    return RedisPools(
        live=Redis.from_url(settings.live_url.get_secret_value()),
        bus=Redis.from_url(settings.bus_url.get_secret_value()),
        broker=Redis.from_url(settings.broker_url.get_secret_value()),
        cache=Redis.from_url(settings.cache_url.get_secret_value()),
    )
