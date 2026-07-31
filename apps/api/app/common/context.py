"""Correlation context — services.md §8.2.

Three identifiers, each answering a different question during an incident:

    request_id       one HTTP request or one WebSocket frame
    correlation_id    the entire causal chain — propagated through the
                      outbox and, once a worker exists, Celery headers
    causation_id      the immediate parent event or command

Bound via contextvars so they propagate through asyncio tasks without being
threaded explicitly through every function signature (services.md §8.1),
and so they can be re-established at a future Celery task boundary from
message headers — the only way a rating update will be traceable back to
the move that caused it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_causation_id: ContextVar[str | None] = ContextVar("causation_id", default=None)


def current_request_id() -> str | None:
    return _request_id.get()


def current_correlation_id() -> str | None:
    return _correlation_id.get()


def current_causation_id() -> str | None:
    return _causation_id.get()


@contextmanager
def bind_context(
    *,
    request_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> Iterator[None]:
    """Bind one or more identifiers for the lifetime of the `with` block,
    restoring the previous value on exit — safe to nest, which matters once
    a Celery task re-establishes context from message headers inside an
    already-bound scope."""
    resets: list[tuple[ContextVar[str | None], object]] = []
    if request_id is not None:
        resets.append((_request_id, _request_id.set(request_id)))
    if correlation_id is not None:
        resets.append((_correlation_id, _correlation_id.set(correlation_id)))
    if causation_id is not None:
        resets.append((_causation_id, _causation_id.set(causation_id)))
    try:
        yield
    finally:
        for var, token in reversed(resets):
            var.reset(token)  # type: ignore[arg-type]
