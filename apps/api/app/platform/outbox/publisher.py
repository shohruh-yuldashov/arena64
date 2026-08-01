"""`OutboxEventPublisher` — the producer side of AD-16, and `NoEventPublisher`.

Thirty lines of real code, and every one of the interesting decisions is
about what this class refuses to do.

**It does not commit.** The event row joins whatever transaction the calling
service already has open, which is the entire pattern: publish-before-commit
and a rollback leaves a consumer acting on a match that does not exist;
publish-after-commit and a crash in between leaves a completed match nothing
will ever rate. Neither is recoverable, because in both cases nothing
recorded that the event was owed.

**It does not deliver.** Nothing here reaches a consumer, a queue or a
socket. A producer holding one of these cannot fan out during a request even
by accident, which is the property A64-013.7 states as "social events must
never be delivered directly from route handlers" — enforced by the type
rather than by review.

**It does not read a clock.** The instant is on the event, because the
instant that matters is when the fact became true and only the producer
knows that. See `DomainEvent.occurred_at`.

## Correlation is read here and nowhere below

`app/common/context.py` holds the request's causal chain in context
variables. This is the boundary where they are read, so the domain objects
underneath stay pure: an `OutboxEntry` built inside a worker and one built
inside a request are the same class behaving the same way, and only their
construction site differs in what it knows.
"""

import logging

from app.common.context import current_causation_id, current_correlation_id
from app.platform.events import DomainEvent
from app.platform.outbox.entry import OutboxEntry
from app.platform.outbox.ports import OutboxRepository

logger = logging.getLogger(__name__)


class OutboxEventPublisher:
    """Stages domain events into the caller's transaction.

    Holds the repository and nothing else — no unit of work, deliberately.
    A publisher that owned a transaction boundary could commit, and the one
    thing this class must be incapable of is committing.
    """

    def __init__(self, outbox: OutboxRepository) -> None:
        self._outbox = outbox

    async def publish(self, event: DomainEvent) -> OutboxEntry:
        """Writes one event into the current transaction.

        Logged at `INFO` as `event_queued` — A64-013.7 asks for it, and it
        is the line that lets an operator follow one fact from the request
        that produced it to the consumer that handled it. **Ids and the
        type only**: the payload is a social fact about named people, and a
        log line carrying it would put the social graph somewhere with
        broader read access than the table it came from (services.md §8.5).
        """
        entry = OutboxEntry.of(
            event,
            correlation_id=current_correlation_id(),
            causation_id=current_causation_id(),
        )
        stored = await self._outbox.enqueue(entry)

        logger.info(
            "event_queued",
            extra={
                "event_id": str(stored.id),
                "event_type": stored.event_type,
                "aggregate_id": str(stored.aggregate_id),
            },
        )
        return stored


class NoEventPublisher:
    """Accepts events and stages nothing — the kill switch's producer half.

    Wired by `OUTBOX_ENABLED=false`, for an outbox that has to be taken out
    of the path without taking the platform with it. The state change still
    commits; only its consequences stop being recorded.

    **Returns a real `OutboxEntry`** rather than `None`, so a producer's
    logging and return type do not branch on which publisher it holds. The
    entry has an id nothing will ever find, which is the honest
    representation of an event that was never made durable.

    Not the default, and not something to run for long: this is the one
    fallback on the platform that loses information rather than degrading
    performance, so unlike `NoPresenceProvider` or `NoSocialGraphCache` it
    is a genuine emergency switch. `WARNING` on every use says so.
    """

    async def publish(self, event: DomainEvent) -> OutboxEntry:
        entry = OutboxEntry.of(event)
        logger.warning(
            "event_discarded_outbox_disabled",
            extra={"event_type": entry.event_type, "aggregate_id": str(entry.aggregate_id)},
        )
        return entry
