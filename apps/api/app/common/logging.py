"""Structured logging setup — CLAUDE.md §8, services.md §8.

The structured-logging *library* is an open question upstream: services.md
§8.1 states "the library choice is pending an ADR; the requirement is
contextvar-based binding that works under both asyncio and Celery's
execution model." This module does not pre-empt that ADR — it configures
the standard library's `logging` with a small JSON formatter and a filter
that injects `app.common.context`'s identifiers, which already satisfies the
stated requirement with zero added dependencies. Swapping in a chosen
library later touches this one module and nothing that calls `logging`.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.common.context import current_causation_id, current_correlation_id, current_request_id
from app.config.environment import Environment


class _ContextFilter(logging.Filter):
    """Injects the three correlation identifiers (services.md §8.2) into
    every log record, so they need never be passed by hand at a call site
    — and so they are never accidentally missing from one."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        record.correlation_id = current_correlation_id()
        record.causation_id = current_causation_id()
        return True


class _JsonFormatter(logging.Formatter):
    """One JSON object per line — CLAUDE.md §8 rule 1. No dependency beyond
    the standard library; see the module docstring."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "correlation_id": getattr(record, "correlation_id", None),
            "causation_id": getattr(record, "causation_id", None),
        }
        if record.exc_info:
            # The exception and its stack, not just the message
            # (CLAUDE.md §8 rule 6).
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _HumanFormatter(logging.Formatter):
    """Readable console output for `local` — dependency-injection.md §2.3."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)s "
            "[req=%(request_id)s corr=%(correlation_id)s] %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = getattr(record, "request_id", None) or "-"
        record.correlation_id = getattr(record, "correlation_id", None) or "-"
        return super().format(record)


def configure_logging(
    *, level: str, environment: Environment, format_override: str | None = None
) -> None:
    """Called once, at process start, before anything else runs — logging
    that isn't configured yet is logging that silently goes to `WARNING`
    on the root default, which is how an incident's first minutes go
    unrecorded.

    `format_override` is `app.config.settings.AppSettings.log_format`: when
    unset, the environment's default applies (dependency-injection.md §2.3).
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    use_human = (
        format_override == "human"
        if format_override is not None
        else environment.uses_human_readable_logs
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.addFilter(_ContextFilter())
    handler.setFormatter(_HumanFormatter() if use_human else _JsonFormatter())
    root.addHandler(handler)

    # Third-party loggers default to WARNING so they do not drown out
    # Arena64's own structured output (CLAUDE.md §8 rule 5 — log at the
    # boundary, not at every step).
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
