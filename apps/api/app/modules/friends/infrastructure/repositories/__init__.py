"""Adapters satisfying the ports `friends` declares in `application/`."""

from app.modules.friends.infrastructure.repositories.friend_request_repository import (
    SqlAlchemyFriendRequestRepository,
)
from app.modules.friends.infrastructure.repositories.friendship_repository import (
    SqlAlchemyFriendshipRepository,
)

__all__ = ["SqlAlchemyFriendRequestRepository", "SqlAlchemyFriendshipRepository"]
