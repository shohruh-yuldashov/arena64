"""app.core.responses / app.api.responses — the standard success envelope."""

from pydantic import BaseModel

from app.api.responses import build_response
from app.common.context import bind_context
from app.core.responses import ApiResponse, ResponseMeta


class _Payload(BaseModel):
    value: int


def test_api_response_wraps_arbitrary_data() -> None:
    envelope = ApiResponse[_Payload](
        data=_Payload(value=1), meta=ResponseMeta(request_id="r1", correlation_id="c1")
    )
    assert envelope.data.value == 1
    assert envelope.meta.request_id == "r1"


def test_meta_fields_default_to_none_outside_a_bound_context() -> None:
    meta = ResponseMeta()
    assert meta.request_id is None
    assert meta.correlation_id is None


def test_build_response_reads_the_current_bound_context() -> None:
    with bind_context(request_id="req-123", correlation_id="corr-456"):
        envelope = build_response(_Payload(value=2))

    assert envelope.data.value == 2
    assert envelope.meta.request_id == "req-123"
    assert envelope.meta.correlation_id == "corr-456"


def test_build_response_outside_any_bound_context_is_still_valid() -> None:
    # A service or task run outside the HTTP request lifecycle (a script, a
    # future Celery task before it binds its own context) must not crash
    # building a response — it should simply carry no correlation info.
    envelope = build_response(_Payload(value=3))
    assert envelope.meta.request_id is None
    assert envelope.meta.correlation_id is None
