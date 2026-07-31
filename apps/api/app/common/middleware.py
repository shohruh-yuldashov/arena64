"""ASGI middleware — technical plumbing with a real framework dependency,
which is exactly what belongs in `common/` and never in `core/`
(dependency-injection.md §3.3).
"""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.common.context import bind_context
from app.core.constants import CORRELATION_ID_HEADER, REQUEST_ID_HEADER


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Binds a request id and correlation id for the lifetime of one HTTP
    request, and echoes both back on the response — CLAUDE.md §8 rule 4:
    "every log carries correlation context... so a single interaction can
    be reconstructed."

    A `request_id` is always minted fresh. A `correlation_id` is accepted
    from an inbound header when the caller already has a causal chain in
    progress, and minted otherwise (services.md §8.2).
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or request_id

        with bind_context(request_id=request_id, correlation_id=correlation_id):
            response = await call_next(request)

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
