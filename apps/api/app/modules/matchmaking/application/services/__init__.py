"""`matchmaking`'s application services — six, split by capability.

`QueueService` owns a player's own ticket: enter a pool, leave one, read it,
requeue it, expire it. `PairingService` (A64-015.3) owns what a *scan* does
with other people's. `PairingReconciliationService` (A64-015.4) owns what
happens when one of those scans dies halfway.

A64-015.5 adds three, and each is a consumer or a job rather than a use case
a request reaches:

    MatchOutcomeService      the acceptance-failure policy (§1), applied to
                             a handshake that completed and failed
    PendingMatchNotifier     realtime delivery of a pending match (§4)
    QueueRetentionService    letting go of the history (§8)

Six classes rather than methods on one, exactly as A64-015.1 predicted:
what differs is the capability, and a service that could queue a player,
create a match on their behalf, rewrite a reservation's outcome, bar them
from the queue *and* delete their history would hand every caller the union
of all five. `QueueService` reaches the HTTP layer; the other five never do
— their callers are background tasks and outbox consumers.
"""

from app.modules.matchmaking.application.services.match_outcome_service import (
    MatchOutcomeService,
)
from app.modules.matchmaking.application.services.pairing_service import (
    PairingOutcome,
    PairingService,
)
from app.modules.matchmaking.application.services.pending_match_notifier import (
    PendingMatchNotifier,
)
from app.modules.matchmaking.application.services.queue_retention_service import (
    QueueRetentionPolicy,
    QueueRetentionResult,
    QueueRetentionService,
    queue_retention_policy,
)
from app.modules.matchmaking.application.services.queue_service import (
    ExpirySweep,
    QueueService,
)
from app.modules.matchmaking.application.services.reconciliation_service import (
    PairingReconciliationService,
    ReconciliationOutcome,
)

__all__ = [
    "ExpirySweep",
    "MatchOutcomeService",
    "PairingOutcome",
    "PairingReconciliationService",
    "PairingService",
    "PendingMatchNotifier",
    "QueueRetentionPolicy",
    "QueueRetentionResult",
    "QueueRetentionService",
    "QueueService",
    "ReconciliationOutcome",
    "queue_retention_policy",
]
