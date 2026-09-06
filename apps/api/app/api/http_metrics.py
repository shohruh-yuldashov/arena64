"""HTTP request metrics — A64-028.6 §3.

## The gap this closes

A64-028.1 recorded that nothing could be observed in production. The
instrumentation audit that opened this task found the sharper version: the
platform had 41 metrics and **not one of them was about an HTTP request**.
Every gateway frame, every pairing scan and every notification was counted;
whether the API answered a request, how long it took, and whether it
answered `500` were not. `configure_logging` also pins `uvicorn.access` to
`WARNING`, so there was no request log either.

Three metrics, which is the smallest set an operator can run a service on:
a rate, an error rate and a latency distribution — plus in-flight, which is
the one that distinguishes "slow" from "stuck" while it is happening.

## Labels, and the one that is dangerous

`method` and `status_class` are closed sets. `route` is **the FastAPI route
template** (`/api/v1/matches/{match_id}`), never `request.url.path`, because
the path carries identifiers and a label per match id is how a metrics
backend is taken down by its own client.

A request that matches no route has no template. It is labelled `unmatched`
rather than by its path — a 404 sweep from a scanner would otherwise mint a
series per probed URL, which is a denial of service against the monitoring
system that anyone on the internet could perform.

`status_class` rather than the code: `2xx`, `4xx`, `5xx` answer the question
an alert asks, and the exact code is in the log line beside the request id.
"""

import time
from collections.abc import Mapping
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.platform.metrics.ports import MetricsRecorder

REQUESTS: Final = "http.requests_total"
REQUEST_DURATION: Final = "http.request_duration_seconds"
REQUESTS_IN_FLIGHT: Final = "http.requests_in_flight"
UNHANDLED: Final = "http.unhandled_exceptions_total"

#: The label a request that matched no route carries. See the module
#: docstring on why this is not the path.
UNMATCHED: Final = "unmatched"


class InFlight:
    """How many requests are being served right now.

    A separate object rather than a field on the middleware, because
    `app.add_middleware` **constructs its own instance** when it builds the
    stack: a composition root that also constructed one to read would be
    reading a counter no request ever touches. This is the shared thing both
    ends hold, and holding it is the only way the gauge can be truthful.

    Read at scrape time rather than pushed as an observation — a depth is a
    value that *is*, not a thing that happened, and
    `platform/metrics/prometheus.py` says why that distinction gets its own
    mechanism.
    """

    def __init__(self) -> None:
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def enter(self) -> None:
        self._count += 1

    def leave(self) -> None:
        self._count -= 1


class HttpMetricsMiddleware(BaseHTTPMiddleware):
    """Counts and times every request, including the ones that raise.

    Placed so that it wraps the exception handlers: a request that ends as an
    unhandled `500` is exactly the request an alert cares about, and a
    middleware inside the handler would record it as whatever the handler
    turned it into.
    """

    def __init__(self, app: ASGIApp, *, metrics: MetricsRecorder, in_flight: "InFlight") -> None:
        super().__init__(app)
        self._metrics = metrics
        self._in_flight = in_flight

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method
        started = time.perf_counter()
        self._in_flight.enter()
        try:
            response = await call_next(request)
        except Exception:
            # Counted before it is re-raised, because the exception handlers
            # above turn it into a response this middleware never sees as an
            # error. An unhandled exception is a different fact from a 500
            # the application chose to return.
            route = _route_of(request)
            self._metrics.increment(UNHANDLED, labels={"route": route, "method": method})
            self._record(route, method, "5xx", started)
            raise
        finally:
            self._in_flight.leave()

        self._record(_route_of(request), method, _status_class(response.status_code), started)
        return response

    def _record(self, route: str, method: str, status_class: str, started: float) -> None:
        labels = {"route": route, "method": method, "status_class": status_class}
        self._metrics.increment(REQUESTS, labels=labels)
        self._metrics.observe(REQUEST_DURATION, time.perf_counter() - started, labels=labels)


def _status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


def _route_of(request: Request) -> str:
    """The request's path with its identifiers put back as placeholders.

    `/api/v1/matches/6f2a…` becomes `/api/v1/matches/{match_id}`.

    ## Why it is rebuilt rather than read

    Two obvious sources both fail here. Walking `app.routes` before dispatch
    does not work because this application mounts its routers as nested
    routers, so the top level holds router objects and every request came
    out labelled `unmatched` — silently, with the label that hides the
    problem. Reading `scope["route"].path` afterwards *does* find a
    template, but a nested router reports **its own** path, so
    `/api/v1/time-controls` arrived as `/time-controls` and two routers
    sharing a sub-path would have become one series.

    Starlette leaves both halves in the scope after routing: the real path,
    and the `path_params` it extracted from it. Substituting each value back
    for its name reconstructs the full template without knowing anything
    about how the routers were assembled — which is also what makes it
    survive the next change to that assembly.

    ## Cardinality

    A request that matched no route has no params, and is labelled
    `unmatched` rather than by its path. A 404 sweep from a scanner would
    otherwise mint one series per probed URL, which is a denial of service
    against the metrics backend that anyone on the internet could perform.
    """
    if request.scope.get("route") is None:
        return UNMATCHED
    path = str(request.scope.get("path", ""))
    params: Mapping[str, object] = request.scope.get("path_params") or {}
    for name, value in params.items():
        path = path.replace(str(value), "{" + name + "}", 1)
    return path


__all__ = [
    "REQUESTS",
    "REQUESTS_IN_FLIGHT",
    "REQUEST_DURATION",
    "UNHANDLED",
    "UNMATCHED",
    "HttpMetricsMiddleware",
    "InFlight",
]
