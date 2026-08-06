"""SQLAlchemy adapters for `notifications`' ports."""

from app.modules.notifications.infrastructure.repositories.notification_repository import (
    SqlAlchemyNotificationRepository,
)
from app.modules.notifications.infrastructure.repositories.preference_repository import (
    SqlAlchemyNotificationPreferenceRepository,
)

__all__ = [
    "SqlAlchemyNotificationPreferenceRepository",
    "SqlAlchemyNotificationRepository",
]
