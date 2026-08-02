"""The transactional outbox — AD-16, AD-17, domain-model.md §13.5.

    entry.py       `OutboxEntry`, the aggregate
    ports.py       the five protocols
    models.py      `platform.outbox`, `platform.processed_event`
    repository.py  the SQLAlchemy adapters, including the `SKIP LOCKED` claim
    publisher.py   the producer side — stages events into the caller's
                   transaction and can do nothing else
    relay.py       one tick: claim, route, deliver, record
    worker.py      the loop that ticks the relay
    retention.py   the horizon: what the log stops keeping, and when

A producer imports `EventPublisher` and `DomainEvent` and nothing else. A
consumer implements `EventHandler`. Everything below that line is the
composition root's.
"""

from app.platform.outbox.entry import OutboxEntry
from app.platform.outbox.isolation import (
    DEFAULT_CONSUMER_TIMEOUT_SECONDS,
    ConsumerPolicies,
    ConsumerPolicy,
)
from app.platform.outbox.models import PLATFORM_SCHEMA, OutboxModel, ProcessedEventModel
from app.platform.outbox.ports import (
    EventFailure,
    EventHandler,
    EventPublisher,
    OutboxRepository,
    OutboxRetentionStore,
    ProcessedEventStore,
)
from app.platform.outbox.publisher import NoEventPublisher, OutboxEventPublisher
from app.platform.outbox.relay import DeliveryFailure, OutboxRelay, RelayTick
from app.platform.outbox.repository import (
    SqlAlchemyOutboxRepository,
    SqlAlchemyOutboxRetentionStore,
    SqlAlchemyProcessedEventStore,
)
from app.platform.outbox.retention import (
    MAINTENANCE_QUEUE,
    OUTBOX_PRUNE_TASK,
    OutboxPruner,
    OutboxRetentionTask,
    PruneResult,
    RetentionPolicy,
    prune_request,
    retention_policy,
)
from app.platform.outbox.worker import OutboxWorker, worker_identity

__all__ = [
    "DEFAULT_CONSUMER_TIMEOUT_SECONDS",
    "ConsumerPolicies",
    "ConsumerPolicy",
    "MAINTENANCE_QUEUE",
    "OUTBOX_PRUNE_TASK",
    "PLATFORM_SCHEMA",
    "DeliveryFailure",
    "EventFailure",
    "EventHandler",
    "EventPublisher",
    "NoEventPublisher",
    "OutboxEntry",
    "OutboxEventPublisher",
    "OutboxModel",
    "OutboxPruner",
    "OutboxRelay",
    "OutboxRepository",
    "OutboxRetentionStore",
    "OutboxRetentionTask",
    "OutboxWorker",
    "ProcessedEventModel",
    "ProcessedEventStore",
    "PruneResult",
    "RelayTick",
    "RetentionPolicy",
    "SqlAlchemyOutboxRepository",
    "SqlAlchemyOutboxRetentionStore",
    "SqlAlchemyProcessedEventStore",
    "prune_request",
    "retention_policy",
    "worker_identity",
]
