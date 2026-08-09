"""SQLAlchemy adapters for `notifications`' ports."""

from app.modules.notifications.infrastructure.repositories.administration import (
    SqlAlchemyAdministrativeNotificationDirectory,
)
from app.modules.notifications.infrastructure.repositories.email_delivery_repository import (
    SqlAlchemyEmailDeliveryRepository,
)
from app.modules.notifications.infrastructure.repositories.notification_repository import (
    SqlAlchemyNotificationRepository,
)
from app.modules.notifications.infrastructure.repositories.preference_repository import (
    SqlAlchemyNotificationPreferenceRepository,
)
from app.modules.notifications.infrastructure.repositories.push_delivery_repository import (
    SqlAlchemyPushDeliveryRepository,
)
from app.modules.notifications.infrastructure.repositories.push_subscription_repository import (
    SqlAlchemyPushSubscriptionRepository,
)

__all__ = [
    "SqlAlchemyAdministrativeNotificationDirectory",
    "SqlAlchemyEmailDeliveryRepository",
    "SqlAlchemyNotificationPreferenceRepository",
    "SqlAlchemyNotificationRepository",
    "SqlAlchemyPushDeliveryRepository",
    "SqlAlchemyPushSubscriptionRepository",
]
