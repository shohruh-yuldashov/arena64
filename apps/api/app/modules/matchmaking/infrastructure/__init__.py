"""`matchmaking`'s adapters — the only layer here that knows PostgreSQL,
SQLAlchemy or a session exists.

    models.py            the `matchmaking` schema and `queue_ticket`
    repositories/        `SqlAlchemyQueueRepository`, incl. the SKIP LOCKED claim
    rating_providers.py  the provisional `RatingSnapshotProvider`
    tasks.py             `QueueExpiryTask`, the background half of `expires_at`

Everything satisfies a port declared in `application/` (AD-06), so a use
case names a contract and never one of these classes.
"""

from app.modules.matchmaking.infrastructure.models import (
    MATCHMAKING_SCHEMA,
    QueueTicketModel,
)
from app.modules.matchmaking.infrastructure.rating_providers import ProvisionalRatingProvider
from app.modules.matchmaking.infrastructure.repositories import SqlAlchemyQueueRepository
from app.modules.matchmaking.infrastructure.tasks import (
    MATCHMAKING_QUEUE,
    QUEUE_EXPIRY_TASK,
    QueueExpiryTask,
    QueueServiceFactory,
    expiry_request,
    worker_identity,
)

__all__ = [
    "MATCHMAKING_QUEUE",
    "MATCHMAKING_SCHEMA",
    "QUEUE_EXPIRY_TASK",
    "ProvisionalRatingProvider",
    "QueueExpiryTask",
    "QueueServiceFactory",
    "QueueTicketModel",
    "SqlAlchemyQueueRepository",
    "expiry_request",
    "worker_identity",
]
