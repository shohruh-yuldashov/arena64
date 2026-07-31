"""ASGI middleware — technical plumbing with a real framework dependency,
which is exactly what belongs in `common/` and never in `core/`
(dependency-injection.md §3.3).

Two independent middlewares, not one, even though A64-006 shipped them
combined: services.md §8.2 defines `request_id` and `correlation_id` as
answering different questions and having different generation rules —
`request_id` is *always* minted fresh at the edge; `correlation_id`
propagates a caller's existing causal chain when one is presented. Coupling
them (correlation defaulting to *this middleware's own* request id) made
`CorrelationIdMiddleware` silently depend on `RequestIdMiddleware` having
already run in a specific order — a composability trap for "everything
created here must be generic and reusable." Each now mints its own
identifier if the inbound value is absent; neither reads the other.
"""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.common.context import bind_context
from app.core.constants import CORRELATION_ID_HEADER, REQUEST_ID_HEADER


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Mints a fresh id for every request and echoes it on the response.

    Always fresh, never accepted from the caller — services.md §8.2:
    "Generated: At the edge, or on frame receipt." A client-supplied
    request id would let one client's requests collide with or spoof
    another's in logs; the correlation id (below) is the mechanism for a
    caller to carry its own identifier through the system.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())

        with bind_context(request_id=request_id):
            response = await call_next(request)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Propagates or mints the id for the entire causal chain — services.md
    §8.2: "propagated through the outbox and Celery headers." Accepted from
    an inbound header when the caller already has one in progress (a
    browser request that is itself downstream of another correlated
    action), minted independently otherwise. Deliberately does not read
    `RequestIdMiddleware`'s value — see this module's docstring.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())

        with bind_context(correlation_id=correlation_id):
            response = await call_next(request)

        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
