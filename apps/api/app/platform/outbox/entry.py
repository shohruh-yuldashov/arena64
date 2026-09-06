"""`OutboxEntry` — domain-model.md §13.5, the aggregate root AD-16 names.

One row is one event, made as durable as the fact that caused it. The
aggregate is trivial by design — there is no invariant here beyond "an entry
that has been published is not claimed again" — and that is the point:
complexity in the outbox is complexity between a business transaction and
its consequences.

## The publication state machine

    pending      published_at is null, attempt_count < max, due now
    claimed      a relay has taken it; other relays skip it (SKIP LOCKED)
    published    published_at set. Retained, never deleted — AD-17 makes
                 this table the durable event log projections rebuild from
    exhausted    attempt_count has reached the ceiling. Still unpublished,
                 so it stays visible in the backlog metric rather than
                 disappearing into a dead-letter table nobody watches

`exhausted` is a *derived* state, not a column, and that is deliberate. A
`failed_at` column would let a monitoring query miss it — "oldest
unpublished row" is the one number an operator already watches
(system-design.md §9), and an event that gave up should make that number
grow, loudly, rather than tidy itself away.

## Why `attempt_count` counts claims and not failures

It is incremented when the row is *claimed*, before the handler runs, so a
relay that dies mid-handler still burns an attempt. Counting only recorded
failures would let a consistently-crashing consumer retry the same event
forever — the failure that never gets recorded is exactly the one that
matters.
"""

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.platform.events import DomainEvent


@dataclass(frozen=True)
class OutboxEntry:
    """One durable event, with its publication state.

    Frozen, like every aggregate on this platform: a repository returns a
    value a caller cannot quietly mutate into disagreement with its row.
    State changes go through the repository's named writes
    (`mark_published`, `mark_failed`), which are the only two transitions
    that exist.
    """

    id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    event_version: int
    payload: dict[str, Any]
    occurred_at: datetime
    correlation_id: str | None = None
    causation_id: str | None = None
    published_at: datetime | None = None
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    claimed_at: datetime | None = None
    claimed_by: str | None = None
    last_error: str | None = field(default=None)

    @classmethod
    def of(
        cls,
        event: DomainEvent,
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> "OutboxEntry":
        """An entry from the event that will be written to it.

        The **id is generated here**, not by the database (DB-07), which is
        what lets a producer log the event id in the same request that
        emitted it and an operator follow one fact from an HTTP log line to
        an outbox row to a consumer's ledger entry.

        `occurred_at` comes from the event rather than from a clock this
        class would have to hold: the event already carries the instant its
        fact became true, and a second reading here would record when the
        row was built instead — a different question, and the wrong one for
        an ordering key.

        `correlation_id` and `causation_id` are threaded from the request
        context by the publisher, not read from a context variable here: a
        domain object that reached into a `ContextVar` would behave
        differently inside a worker than inside a request, which is the
        action at a distance CLAUDE.md §2.1 rules out.
        """
        return cls(
            id=generate_uuid7(),
            aggregate_type=type(event).aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=type(event).event_type,
            event_version=type(event).event_version,
            payload=event.payload(),
            occurred_at=event.occurred_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            # `next_attempt_at` is left null rather than set to `occurred_at`
            # so that "never attempted" and "attempted and backed off to
            # exactly now" stay distinguishable in the table. The claim
            # predicate treats null as due.
            next_attempt_at=None,
        )

    @property
    def is_published(self) -> bool:
        return self.published_at is not None


@dataclass(frozen=True, slots=True)
class OutboxBacklog:
    """What is waiting, what is lost, and how old the oldest wait is.

    A64-028.6 §3. Three numbers rather than one, because they fail
    differently: a large `retryable` that is falling is a busy platform, a
    small one whose `oldest_pending_age_seconds` keeps climbing is a stuck
    consumer, and any rise in `exhausted` is permanent loss that no amount
    of waiting fixes.
    """

    retryable: int
    exhausted: int
    oldest_pending_age_seconds: float


class BacklogSnapshot:
    """The last backlog reading, held for the exporter to publish.

    A64-028.6 §3. Mutable and process-local by design: the relay writes it
    once per tick from the session it already has open, and the exporter
    reads it during a scrape. The alternative — a gauge source that opens
    its own session — makes the monitoring into the load, and a reading at
    most one poll interval old is finer than any scrape interval worth
    configuring.

    Starts empty rather than zeroed, so a process that has not ticked yet
    publishes nothing instead of publishing "no backlog", which is the
    reading an operator would most regret trusting.
    """

    def __init__(self) -> None:
        self._value: OutboxBacklog | None = None

    @property
    def value(self) -> OutboxBacklog | None:
        return self._value

    def set(self, backlog: OutboxBacklog) -> None:
        self._value = backlog


@lru_cache(maxsize=1)
def process_backlog() -> BacklogSnapshot:
    """The one snapshot a process has.

    Cached rather than a module global, for the reason `process_metrics()`
    gives: the sharing is visible at the call site, and a test can
    `process_backlog.cache_clear()` to get a process with no reading. Two
    callers need the same object and neither can pass it to the other — the
    relay worker is built inside the lifespan and the gauge is registered
    while the app is being assembled, which is strictly earlier.
    """
    return BacklogSnapshot()
