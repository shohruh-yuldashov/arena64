"""`matchmaking`'s adapters — the only layer here that knows PostgreSQL,
SQLAlchemy or a session exists.

    models.py               the schema, `queue_ticket` and `queue_cooldown`
    repositories/           the three adapters, incl. the SKIP LOCKED claims
    rating_providers.py     `RatingSnapshotProvider` over `rating.public`
    pending_match_sinks.py  where a realtime offer goes (A64-015.5 §4)
    tasks.py                the four `platform.tasks` handlers

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
    QueueCooldownAuditModel,
    QueueCooldownModel,
    QueueTicketModel,
    ReconciliationTimelineModel,
)
from app.modules.matchmaking.infrastructure.pending_match_sinks import (
    LoggingPendingMatchSink,
    NullPendingMatchSink,
)
from app.modules.matchmaking.infrastructure.rating_providers import PublishedRatingProvider
from app.modules.matchmaking.infrastructure.repositories import (
    SqlAlchemyCooldownAuditRepository,
    SqlAlchemyCooldownRepository,
    SqlAlchemyQueueRepository,
    SqlAlchemyQueueRetentionStore,
    SqlAlchemyReconciliationTimelineRepository,
)
from app.modules.matchmaking.infrastructure.tasks import (
    MAINTENANCE_QUEUE,
    MATCHMAKING_QUEUE,
    PAIRING_POOL_KEY,
    PAIRING_TASK,
    QUEUE_EXPIRY_TASK,
    QUEUE_RETENTION_TASK,
    RECONCILIATION_TASK,
    ChallengeExpiryTask,
    PairingReconciliationTask,
    PairingServiceFactory,
    PairingTask,
    QueueExpiryTask,
    QueueRetentionServiceFactory,
    QueueRetentionTask,
    QueueServiceFactory,
    ReconciliationServiceFactory,
    challenge_expiry_request,
    expiry_request,
    pairing_request,
    queue_retention_request,
    reconciliation_request,
    worker_identity,
)

__all__ = [
    "MAINTENANCE_QUEUE",
    "MATCHMAKING_QUEUE",
    "MATCHMAKING_SCHEMA",
    "PAIRING_POOL_KEY",
    "PAIRING_TASK",
    "QUEUE_EXPIRY_TASK",
    "QUEUE_RETENTION_TASK",
    "RECONCILIATION_TASK",
    "LoggingPendingMatchSink",
    "NullPendingMatchSink",
    "PairingReconciliationTask",
    "PairingServiceFactory",
    "PairingTask",
    "PublishedRatingProvider",
    "QueueExpiryTask",
    "QueueRetentionServiceFactory",
    "ChallengeExpiryTask",
    "QueueRetentionTask",
    "QueueServiceFactory",
    "QueueCooldownAuditModel",
    "QueueCooldownModel",
    "QueueTicketModel",
    "ReconciliationTimelineModel",
    "ReconciliationServiceFactory",
    "SqlAlchemyCooldownAuditRepository",
    "SqlAlchemyCooldownRepository",
    "SqlAlchemyQueueRepository",
    "SqlAlchemyQueueRetentionStore",
    "SqlAlchemyReconciliationTimelineRepository",
    "expiry_request",
    "pairing_request",
    "challenge_expiry_request",
    "queue_retention_request",
    "reconciliation_request",
    "worker_identity",
]
