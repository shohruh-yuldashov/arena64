"""`matchmaking`'s repository adapters.

    SqlAlchemyQueueRepository     the `queue_ticket` aggregate root
    SqlAlchemyCooldownRepository  the `queue_cooldown` relation (A64-015.5)
    SqlAlchemyQueueRetentionStore the bounded deletes, and nothing else

The third is not a repository in the aggregate sense and is here anyway,
because "adapter that talks to `matchmaking`'s tables" is what this package
is. It is a separate class from the first for the reason the ports are
separate protocols: the object that can delete a ticket must not also be
able to resolve one.
"""

from app.modules.matchmaking.infrastructure.repositories.cooldown_repository import (
    SqlAlchemyCooldownRepository,
)
from app.modules.matchmaking.infrastructure.repositories.queue_repository import (
    SqlAlchemyQueueRepository,
)
from app.modules.matchmaking.infrastructure.repositories.queue_retention_store import (
    SqlAlchemyQueueRetentionStore,
)

__all__ = [
    "SqlAlchemyCooldownRepository",
    "SqlAlchemyQueueRepository",
    "SqlAlchemyQueueRetentionStore",
]
