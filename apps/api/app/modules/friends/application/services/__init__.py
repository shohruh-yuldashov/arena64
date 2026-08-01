"""Application services — one class per cohesive set of use cases
(services.md §3)."""

from app.modules.friends.application.services.friend_request_service import (
    FriendRequestService,
)

__all__ = ["FriendRequestService"]
