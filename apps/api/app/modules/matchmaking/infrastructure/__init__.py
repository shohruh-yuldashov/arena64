"""`matchmaking`'s adapters — the only layer here that knows PostgreSQL,
SQLAlchemy or a session exists.

    models.py            the `matchmaking` schema and `queue_ticket`
    repositories/        `SqlAlchemyQueueRepository`, incl. the SKIP LOCKED claims
    rating_providers.py  the provisional `RatingSnapshotProvider`
    tasks.py             `QueueExpiryTask`, `PairingTask` and
                         `PairingReconciliationTask`

Everything satisfies a port declared in `application/` (AD-06), so a use
case names a contract and never one of these classes.

`opponent_providers.py` is **gone** as of A64-015.4, and its absence is the
point rather than a deletion worth mourning: `NoRecentOpponents` existed
because `game` had no match history to read, and A64-015.4 gave it one. The
port is now satisfied by `game.public.RecentOpponentReader` — exactly the
swap A64-015.3 predicted, one line in the composition root and nothing in
`PairingService`.
"""

from app.modules.matchmaking.infrastructure.models import (
    MATCHMAKING_SCHEMA,
    QueueTicketModel,
)
from app.modules.matchmaking.infrastructure.rating_providers import ProvisionalRatingProvider
from app.modules.matchmaking.infrastructure.repositories import SqlAlchemyQueueRepository
from app.modules.matchmaking.infrastructure.tasks import (
    MATCHMAKING_QUEUE,
    PAIRING_POOL_KEY,
    PAIRING_TASK,
    QUEUE_EXPIRY_TASK,
    RECONCILIATION_TASK,
    PairingReconciliationTask,
    PairingServiceFactory,
    PairingTask,
    QueueExpiryTask,
    QueueServiceFactory,
    ReconciliationServiceFactory,
    expiry_request,
    pairing_request,
    reconciliation_request,
    worker_identity,
)

__all__ = [
    "MATCHMAKING_QUEUE",
    "MATCHMAKING_SCHEMA",
    "PAIRING_POOL_KEY",
    "PAIRING_TASK",
    "QUEUE_EXPIRY_TASK",
    "RECONCILIATION_TASK",
    "PairingReconciliationTask",
    "PairingServiceFactory",
    "PairingTask",
    "ProvisionalRatingProvider",
    "QueueExpiryTask",
    "QueueServiceFactory",
    "QueueTicketModel",
    "ReconciliationServiceFactory",
    "SqlAlchemyQueueRepository",
    "expiry_request",
    "pairing_request",
    "reconciliation_request",
    "worker_identity",
]
