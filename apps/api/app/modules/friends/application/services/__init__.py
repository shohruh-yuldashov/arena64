"""Application services — one class per cohesive set of use cases
(services.md §3)."""

from app.modules.friends.application.services.friend_request_service import (
    FriendRequestService,
)
from app.modules.friends.application.services.friendship_service import FriendshipService

__all__ = ["FriendRequestService", "FriendshipService"]
