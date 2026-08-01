"""Wire schemas for the `friends` module."""

from app.modules.friends.presentation.schemas.friend_request import (
    FriendRequestResponse,
    SendFriendRequestRequest,
)
from app.modules.friends.presentation.schemas.friendship import (
    FriendCountResponse,
    FriendResponse,
    FriendshipDetailsResponse,
)

__all__ = [
    "FriendCountResponse",
    "FriendRequestResponse",
    "FriendResponse",
    "FriendshipDetailsResponse",
    "SendFriendRequestRequest",
]
