"""SQLAlchemy adapters for `notifications`' ports."""

from app.modules.notifications.infrastructure.repositories.notification_repository import (
    SqlAlchemyNotificationRepository,
)

__all__ = ["SqlAlchemyNotificationRepository"]
