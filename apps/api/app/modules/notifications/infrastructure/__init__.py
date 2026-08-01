"""Adapters for `notifications`' ports, and the relay's entry point."""

from app.modules.notifications.infrastructure.session_scoped_handler import (
    SessionScopedNotificationHandler,
)
from app.modules.notifications.infrastructure.sinks import (
    LoggingNotificationSink,
    NullNotificationSink,
)

__all__ = [
    "LoggingNotificationSink",
    "NullNotificationSink",
    "SessionScopedNotificationHandler",
]
