"""Wire schemas for the `friends` module."""

from app.modules.friends.presentation.schemas.friend_request import (
    FriendRequestResponse,
    SendFriendRequestRequest,
)

__all__ = ["FriendRequestResponse", "SendFriendRequestRequest"]
