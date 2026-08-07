"""SQLAlchemy adapters for `notifications`' ports."""

from app.modules.notifications.infrastructure.repositories.email_delivery_repository import (
    SqlAlchemyEmailDeliveryRepository,
)
from app.modules.notifications.infrastructure.repositories.notification_repository import (
    SqlAlchemyNotificationRepository,
)
from app.modules.notifications.infrastructure.repositories.preference_repository import (
    SqlAlchemyNotificationPreferenceRepository,
)

__all__ = [
    "SqlAlchemyEmailDeliveryRepository",
    "SqlAlchemyNotificationPreferenceRepository",
    "SqlAlchemyNotificationRepository",
]
