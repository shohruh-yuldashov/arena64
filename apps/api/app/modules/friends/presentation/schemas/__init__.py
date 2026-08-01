"""Wire schemas for the `friends` module."""

from app.modules.friends.presentation.schemas.block import (
    BlockedPlayerResponse,
    BlockPlayerRequest,
)
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
    "BlockPlayerRequest",
    "BlockedPlayerResponse",
    "FriendCountResponse",
    "FriendRequestResponse",
    "FriendResponse",
    "FriendshipDetailsResponse",
    "SendFriendRequestRequest",
]
