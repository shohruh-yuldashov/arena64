"""Adapters satisfying the ports `friends` declares in `application/`."""

from app.modules.friends.infrastructure.repositories.friend_request_repository import (
    SqlAlchemyFriendRequestRepository,
)

__all__ = ["SqlAlchemyFriendRequestRepository"]
