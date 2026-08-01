"""`matchmaking`'s wire schemas."""

from app.modules.matchmaking.presentation.schemas.queue import (
    JoinQueueRequest,
    QueueTicketResponse,
)

__all__ = ["JoinQueueRequest", "QueueTicketResponse"]
