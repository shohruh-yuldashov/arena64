"""The transactional outbox — AD-16, AD-17, domain-model.md §13.5.

    entry.py       `OutboxEntry`, the aggregate
    ports.py       the four protocols
    models.py      `platform.outbox`, `platform.processed_event`
    repository.py  the SQLAlchemy adapters, including the `SKIP LOCKED` claim
    publisher.py   the producer side — stages events into the caller's
                   transaction and can do nothing else
    relay.py       one tick: claim, route, deliver, record
    worker.py      the loop that ticks the relay

A producer imports `EventPublisher` and `DomainEvent` and nothing else. A
consumer implements `EventHandler`. Everything below that line is the
composition root's.
"""

from app.platform.outbox.entry import OutboxEntry
from app.platform.outbox.models import PLATFORM_SCHEMA, OutboxModel, ProcessedEventModel
from app.platform.outbox.ports import (
    EventFailure,
    EventHandler,
    EventPublisher,
    OutboxRepository,
    ProcessedEventStore,
)
from app.platform.outbox.publisher import NoEventPublisher, OutboxEventPublisher
from app.platform.outbox.relay import DeliveryFailure, OutboxRelay, RelayTick
from app.platform.outbox.repository import (
    SqlAlchemyOutboxRepository,
    SqlAlchemyProcessedEventStore,
)
from app.platform.outbox.worker import OutboxWorker, worker_identity

__all__ = [
    "PLATFORM_SCHEMA",
    "DeliveryFailure",
    "EventFailure",
    "EventHandler",
    "EventPublisher",
    "NoEventPublisher",
    "OutboxEntry",
    "OutboxEventPublisher",
    "OutboxModel",
    "OutboxRelay",
    "OutboxRepository",
    "OutboxWorker",
    "ProcessedEventModel",
    "ProcessedEventStore",
    "RelayTick",
    "SqlAlchemyOutboxRepository",
    "SqlAlchemyProcessedEventStore",
    "worker_identity",
]
