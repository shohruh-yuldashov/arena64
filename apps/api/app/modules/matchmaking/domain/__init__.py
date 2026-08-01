"""`matchmaking`'s domain — the rules of waiting for an opponent.

    queue_ticket.py   `QueueTicket`, the aggregate root, and its enums
    events.py         the three transitions, as durable facts
    exceptions/       this module's typed failures

Framework-free (architecture.md §8) and enforced: `.importlinter` fails if
anything here imports SQLAlchemy, FastAPI, Starlette or Redis. Time arrives
as an argument (AD-07) — nothing below reads a clock.

Nothing about *pairing* lives here, deliberately. A64-014.1 builds the
foundation every matchmaking workflow stands on and stops; the rating
window, opponent eligibility (QT-3) and the two-phase claim (QT-4) are
A64-014.2's, and each is a property of a scan over tickets rather than of a
ticket.
"""

from app.modules.matchmaking.domain.events import (
    QUEUE_TICKET_AGGREGATE,
    QueueTicketCancelled,
    QueueTicketEnqueued,
    QueueTicketExpired,
)
from app.modules.matchmaking.domain.queue_pool import QueueType, Region
from app.modules.matchmaking.domain.queue_ticket import (
    PROVISIONAL_RATING,
    QueueSnapshot,
    QueueStatus,
    QueueTicket,
)

__all__ = [
    "PROVISIONAL_RATING",
    "QUEUE_TICKET_AGGREGATE",
    "QueueSnapshot",
    "QueueStatus",
    "QueueTicket",
    "QueueTicketCancelled",
    "QueueTicketEnqueued",
    "QueueTicketExpired",
    "QueueType",
    "Region",
]
