"""`matchmaking`'s application services — three, split by capability.

`QueueService` owns a player's own ticket: enter a pool, leave one, read
it, expire it. `PairingService` (A64-015.3) owns what a *scan* does with
other people's: select two, claim them, ask `game` for a match.
`PairingReconciliationService` (A64-015.4) owns what happens when one of
those scans dies halfway.

Three classes rather than methods on one, exactly as A64-015.1 predicted:
what differs is the capability, and a service that could queue a player,
create a match on their behalf *and* rewrite a reservation's outcome would
hand every caller the union of the three. `QueueService` reaches the HTTP
layer; the other two never do — their only callers are background tasks.
"""

from app.modules.matchmaking.application.services.pairing_service import (
    PairingOutcome,
    PairingService,
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
    "PairingOutcome",
    "PairingReconciliationService",
    "PairingService",
    "QueueService",
    "ReconciliationOutcome",
]
