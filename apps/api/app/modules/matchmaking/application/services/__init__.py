"""`matchmaking`'s application services — one, and it owns the queue.

`QueueService` is the only entry point into this module from outside
(architecture.md §9), and it is the only transaction owner. A64-014.2 adds a
second — the pairing service — rather than methods here: what differs is the
capability, and a service that can both queue a player and create a match on
their behalf would hand every caller the union of the two.
"""

from app.modules.matchmaking.application.services.queue_service import (
    ExpirySweep,
    QueueService,
)

__all__ = ["ExpirySweep", "QueueService"]
