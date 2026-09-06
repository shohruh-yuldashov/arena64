"""Health endpoints.

Three, deliberately distinct:

  liveness   "is this process able to serve traffic at all" — must never
             depend on anything that can be slow or down. A dead
             dependency must not make an otherwise-healthy process look
             dead to its orchestrator and get killed for the wrong reason.
  readiness  "can this process actually do its job right now" — does
             depend on Postgres and Redis, because a process that cannot
             reach either genuinely should be taken out of rotation, and
             on the drain flag, because a deploy needs a way to say so.
  drain      "stop sending me new work" — the operator route a deploy calls
             before it signals the process. See `api/lifecycle.py`.

## Readiness answers with the status line — A64-028.6 §9

Until this task readiness returned **HTTP 200** with `status: "degraded"`
even when both dependencies were unreachable. A load balancer reads the
status line; nothing in the fleet parses the body. So an instance with no
database stayed in rotation and kept failing requests, which is exactly the
condition the endpoint exists to prevent (P1-5).

It now returns **503** when a required dependency is down or the instance is
draining. The diagnostic body is unchanged and still says *which*, because
that is what makes the probe useful to a human as well as to a balancer.

## Which dependencies are required, and which are not

  PostgreSQL   **required.** It is the source of truth; a process that
               cannot reach it can serve almost nothing correctly.
  Redis        **required.** Not because every request needs it, but
               because the ones that do are the realtime path — tickets,
               rooms, the cross-instance bus — and an instance serving HTTP
               while silently unable to deliver a move is worse than one
               that is out of rotation.
  Draining     **required to be false.** The whole point of the flag.

Deliberately **not** checked: the outbox relay, the schedulers, and the
metrics exporter. All three are per-process background work whose failure
is a delivery delay rather than a reason to stop serving requests — and a
readiness probe that failed on a stalled relay would take the whole fleet
out of rotation over a backlog that another instance is already draining.
They are alerted on directly instead (`docs/01-architecture/observability.md`).

## Why the body never grows an error string

A readiness body is served to whatever can reach the port. `postgres: false`
says everything an operator needs and nothing an attacker can use; a DSN, a
driver message or a stack would be a configuration leak on a route that
exists to be probed by machines.
"""

import logging

from fastapi import APIRouter, Header, Response, status
from pydantic import BaseModel

from app.api.deps import DbSessionDep, RedisPoolsDep, ServiceLifecycleDep
from app.api.responses import build_response
from app.api.security import operator_authorised
from app.config.settings import Settings
from app.core.exceptions import AuthenticationFailed
from app.core.responses import ApiResponse
from app.database.health import check_database_connection

logger = logging.getLogger(__name__)

health_router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: str = "ok"


class ReadinessResponse(BaseModel):
    status: str
    postgres: bool
    redis: dict[str, bool]
    draining: bool


class DrainResponse(BaseModel):
    draining: bool
    changed: bool


@health_router.get("")
async def liveness() -> ApiResponse[LivenessResponse]:
    return build_response(LivenessResponse())


@health_router.get("/ready")
async def readiness(
    session: DbSessionDep,
    redis_pools: RedisPoolsDep,
    lifecycle: ServiceLifecycleDep,
    response: Response,
) -> ApiResponse[ReadinessResponse]:
    postgres_ok = await check_database_connection(session)
    redis_ok = await redis_pools.ping_all()

    ready = postgres_ok and all(redis_ok.values()) and not lifecycle.draining
    if not ready:
        # The status line, not a field. See this module's docstring.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return build_response(
        ReadinessResponse(
            status="ok" if ready else "degraded",
            postgres=postgres_ok,
            redis=redis_ok,
            draining=lifecycle.draining,
        )
    )


def build_drain_route(settings: Settings) -> APIRouter:
    """`POST /health/drain`, closed over the operator token.

    A router built at composition rather than a module-level route, for the
    reason `api/observability.py` gives: the guard needs settings, and a
    route reaching for a global to find them is the hidden coupling
    `CLAUDE.md` §2.1 forbids.
    """
    router = APIRouter(prefix="/health", tags=["health"])

    @router.post("/drain", include_in_schema=False)
    async def drain(
        lifecycle: ServiceLifecycleDep, authorization: str | None = Header(default=None)
    ) -> ApiResponse[DrainResponse]:
        if not operator_authorised(authorization, settings):
            logger.warning("drain_request_refused")
            raise AuthenticationFailed("A valid bearer token is required to drain an instance.")
        return build_response(DrainResponse(draining=True, changed=lifecycle.begin_drain()))

    return router
