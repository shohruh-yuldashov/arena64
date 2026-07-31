"""The standard API response envelope — every success response on the
platform takes this shape, so a client writes one unwrapping path
(`apps/web/src/services/response-parser.ts`) instead of one per endpoint.

Deliberately just the two data shapes. Populating `meta` from the current
request needs `app.common.context`, and `core/` may depend on `shared/`
only, never `common/` (dependency-injection.md §3.2's layering — `common/`
depends on `core/`, not the reverse). That factory lives at the interface
layer instead: `app.api.responses.build_response`.
"""

from pydantic import BaseModel


class ResponseMeta(BaseModel):
    """Carried on every response — the same three-identifier discipline as
    the log line that produced it (services.md §8.2), so a player-reported
    bug can be traced from the response they saw straight to the backend
    logs that explain it.
    """

    request_id: str | None = None
    correlation_id: str | None = None


class ApiResponse[T](BaseModel):
    """The standard success envelope. `data` is deliberately generic — a
    single resource, a list, a paginated page (`app.core.pagination`) —
    the envelope doesn't know or care what it carries; only its own two
    fields, `data` and `meta`, are the platform's contract.

    Error responses do **not** use this envelope — see
    `app.api.exception_handlers.ErrorResponse`. Nesting an error under
    `data` would make every client check "did this succeed" by inspecting
    the body instead of the HTTP status, which is the one signal that is
    never ambiguous.
    """

    data: T
    meta: ResponseMeta
