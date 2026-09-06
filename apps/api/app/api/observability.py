"""`/metrics` — the scrape endpoint. A64-028.6 §5.

## Why it is not under `/api/v1`

The same reason `/health` is not: this is not part of the product's API and
is not versioned with it. A monitoring system's scrape target must not move
because the product shipped a v2, and a client of the product must never
find this by walking the API.

## Two boundaries, and neither is enough alone

The route refuses a caller without the bearer token, and the edge refuses
`/metrics` from the public internet (`infrastructure/production/Caddyfile`).
Either alone has a way of being wrong — a mislaid token, a misconfigured
proxy — and the combination is what makes both survivable.

`ObservabilitySettings` refuses to start a production-like tier with the
exporter enabled and neither a token nor an explicit acknowledgement that
the scrape network is trusted, so an *accidentally* open exporter cannot
reach this file.

## Why the failure answers 401 and not 404

A 404 would hide the route, and hiding it is worth nothing: the path is in
this repository and in the Prometheus configuration. What a 404 would cost
is the operator's ability to tell "my token is wrong" from "the exporter is
switched off", which are different incidents with different fixes. Nothing
about the platform is disclosed by admitting that a metrics endpoint exists.
"""

import logging

from fastapi import APIRouter, Header, Response

from app.api.security import operator_authorised
from app.config.settings import Settings
from app.core.exceptions import AuthenticationFailed
from app.platform.metrics.prometheus import CONTENT_TYPE, PrometheusMetrics

logger = logging.getLogger(__name__)


def build_metrics_router(exporter: PrometheusMetrics, settings: Settings) -> APIRouter:
    """The route, closed over the process's exporter.

    Built rather than imported so the exporter is injected: a module-level
    route reaching for the singleton would be the hidden global
    `CLAUDE.md` §2.1 forbids, and would make the endpoint untestable
    without a process-wide recorder.
    """
    router = APIRouter(tags=["observability"])

    @router.get("/metrics", include_in_schema=False)
    async def metrics(authorization: str | None = Header(default=None)) -> Response:
        if not operator_authorised(authorization, settings):
            # WARNING rather than INFO: on a network where only the scraper
            # should be able to reach this, a refused scrape is either a
            # broken monitor or something that should not be there.
            logger.warning("metrics_scrape_refused")
            raise AuthenticationFailed("A valid bearer token is required for /metrics.")
        return Response(content=await exporter.render(), media_type=CONTENT_TYPE)

    return router


__all__ = ["build_metrics_router"]
