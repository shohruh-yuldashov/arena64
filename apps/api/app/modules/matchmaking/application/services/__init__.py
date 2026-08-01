"""`matchmaking`'s application services — two, split by capability.

`QueueService` owns a player's own ticket: enter a pool, leave one, read
it, expire it. `PairingService` (A64-015.3) owns what a *scan* does with
other people's: select two, claim them, ask `game` for a match.

Two classes rather than methods on one, exactly as A64-015.1 predicted:
what differs is the capability, and a service that could both queue a
player and create a match on their behalf would hand every caller the union
of the two. `QueueService` reaches the HTTP layer; `PairingService` never
does — its only caller is a background task.
"""

from app.modules.matchmaking.application.services.pairing_service import (
    PairingOutcome,
    PairingService,
)
from app.modules.matchmaking.application.services.queue_service import (
    ExpirySweep,
    QueueService,
)

__all__ = ["ExpirySweep", "PairingOutcome", "PairingService", "QueueService"]
