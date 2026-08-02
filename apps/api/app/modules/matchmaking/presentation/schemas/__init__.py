"""`matchmaking`'s wire schemas."""

from app.modules.matchmaking.presentation.schemas.matches import (
    OpponentPreview,
    PendingMatchResponse,
)
from app.modules.matchmaking.presentation.schemas.queue import (
    JoinQueueRequest,
    QueueTicketResponse,
)

__all__ = [
    "JoinQueueRequest",
    "OpponentPreview",
    "PendingMatchResponse",
    "QueueTicketResponse",
]
