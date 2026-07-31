"""Health endpoints.

Two, deliberately distinct:

  liveness   "is this process able to serve traffic at all" — must never
             depend on anything that can be slow or down. A dead
             dependency must not make an otherwise-healthy process look
             dead to its orchestrator and get killed for the wrong reason.
  readiness  "can this process actually do its job right now" — does
             depend on Postgres and Redis, because a process that cannot
             reach either genuinely should be taken out of rotation.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import DbSessionDep, RedisPoolsDep
from app.api.responses import build_response
from app.core.responses import ApiResponse

logger = logging.getLogger(__name__)

health_router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: str = "ok"


class ReadinessResponse(BaseModel):
    status: str
    postgres: bool
    redis: dict[str, bool]


@health_router.get("")
async def liveness() -> ApiResponse[LivenessResponse]:
    return build_response(LivenessResponse())


@health_router.get("/ready")
async def readiness(
    session: DbSessionDep, redis_pools: RedisPoolsDep
) -> ApiResponse[ReadinessResponse]:
    try:
        await session.execute(text("SELECT 1"))
        postgres_ok = True
    except Exception:  # noqa: BLE001 — a readiness probe must not raise
        logger.warning("readiness_postgres_check_failed", exc_info=True)
        postgres_ok = False

    redis_ok = await redis_pools.ping_all()

    all_ok = postgres_ok and all(redis_ok.values())
    return build_response(
        ReadinessResponse(
            status="ok" if all_ok else "degraded", postgres=postgres_ok, redis=redis_ok
        )
    )
