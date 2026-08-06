"""Structured logging setup — CLAUDE.md §8, services.md §8.

The structured-logging *library* is an open question upstream: services.md
§8.1 states "the library choice is pending an ADR; the requirement is
contextvar-based binding that works under both asyncio and Celery's
execution model." This module does not pre-empt that ADR — it configures
the standard library's `logging` with a small JSON formatter and a filter
that injects `app.common.context`'s identifiers, which already satisfies the
stated requirement with zero added dependencies. Swapping in a chosen
library later touches this one module and nothing that calls `logging`.

## A64-021.2H: the fields were being thrown away

Both formatters below built their output from a **fixed** set of attributes,
so every `extra={...}` a call site passed — and this codebase passes them
everywhere — reached the handler and was discarded. `event_queued` logged no
event id, `notification_pushed` no outcome, `outbox_tick_completed` no
counts. CLAUDE.md §8 rule 1 asks for key–value or JSON fields "never
interpolated prose that must be regex-parsed later", and prose with the
fields removed is what came out.

It was found while diagnosing a notification that never arrived: every log
line on the path existed, and not one of them said anything. Both formatters
now emit the caller's fields.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.common.context import current_causation_id, current_correlation_id, current_request_id
from app.config.environment import Environment

#: Everything `logging` puts on a record itself, plus what `_ContextFilter`
#: adds and what the fixed payload already names.
#:
#: Anything on a record that is *not* here came from a caller's `extra=`,
#: which is how this codebase carries structured detail — `match_id`,
#: `event_id`, `outcome`, `skipped`. A64-021.2H found that detail was
#: computed at every call site and **emitted by neither formatter**, so
#: every line the platform logged was a bare message: CLAUDE.md §8 rule 1
#: asks for "key–value or JSON fields, never interpolated prose", and prose
#: is exactly what came out.
#:
#: Enumerated rather than discovered from a fresh `LogRecord`, because
#: `logging` sets some attributes only in some paths (`exc_text`,
#: `stack_info`) and a probe record would miss them — leaving them to leak
#: into output the first time an exception is logged.
_RESERVED: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "stacklevel",
        "taskName",
        "thread",
        "threadName",
        # `_ContextFilter`'s, already named by the fixed payload below.
        "request_id",
        "correlation_id",
        "causation_id",
    }
)


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    """The fields a caller passed as `extra=`, and nothing else.

    Sorted, so two lines about the same event read the same way and a diff
    of two logs is about their content rather than about dictionary order.
    """
    return {
        key: value
        for key, value in sorted(record.__dict__.items())
        if key not in _RESERVED and not key.startswith("_")
    }


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
        # The caller's structured fields. Merged **under** the fixed keys
        # above, so an `extra={"level": ...}` cannot rewrite the level a
        # log aggregator filters on.
        for key, value in _extras(record).items():
            payload.setdefault(key, value)
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
        extras = _extras(record)
        record.request_id = getattr(record, "request_id", None) or "-"
        record.correlation_id = getattr(record, "correlation_id", None) or "-"
        line = super().format(record)
        if not extras:
            return line
        # `key=value` after the message rather than before it, so the thing
        # a human scans for stays at a predictable column.
        return f"{line} " + " ".join(f"{key}={value}" for key, value in extras.items())


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
