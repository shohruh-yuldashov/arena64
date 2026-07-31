"""The interface-layer half of `app.core.responses` — populates `meta`
from the request's bound context (`app.common.context`), which `core/`
itself may not import (dependency-injection.md §3.2). Every route builds
its success response through this, the same way every route's errors flow
through `app.api.exception_handlers` — one construction path, not one per
route.
"""

from app.common.context import current_correlation_id, current_request_id
from app.core.responses import ApiResponse, ResponseMeta


def build_response[T](data: T) -> ApiResponse[T]:
    return ApiResponse(
        data=data,
        meta=ResponseMeta(
            request_id=current_request_id(),
            correlation_id=current_correlation_id(),
        ),
    )
