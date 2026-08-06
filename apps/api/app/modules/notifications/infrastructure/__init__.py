"""Adapters for `notifications`' ports, and the relay's session bridge."""

from app.modules.notifications.infrastructure.presence_sweeper_worker import (
    PresenceSweeperWorker,
    SweeperFactory,
)
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyNotificationRepository,
)
from app.modules.notifications.infrastructure.session_scoped_handler import (
    DispatcherFactory,
    SessionScopedNotificationHandler,
)
from app.modules.notifications.infrastructure.sinks import (
    CompositeNotificationSink,
    LoggingNotificationSink,
    NullNotificationAnnouncer,
    NullNotificationSink,
)

__all__ = [
    "CompositeNotificationSink",
    "DispatcherFactory",
    "PresenceSweeperWorker",
    "SweeperFactory",
    "LoggingNotificationSink",
    "NullNotificationAnnouncer",
    "NullNotificationSink",
    "SessionScopedNotificationHandler",
    "SqlAlchemyNotificationRepository",
]
