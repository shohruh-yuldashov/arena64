"""The outbox's ports — AD-06 puts them in the layer that *needs* them, so
a consumer depends on the contract and never on `SqlAlchemyOutboxRepository`.

Four protocols, and the split between them is by capability rather than by
convenience — the same argument every port pair on this platform makes:

    EventPublisher       a producer can *emit*. It cannot claim, publish or
                         read anything back, which is why a friends service
                         holding one cannot accidentally deliver its own
                         event inside the request that caused it
    OutboxRepository     the relay's surface: enqueue, claim, mark. Held by
                         the worker and by nothing on the HTTP path
    ProcessedEventStore  the consumer-side idempotency ledger (§13.6)
    EventHandler         what a consumer implements to be routed to

`EventPublisher` is the one that matters for the platform's shape. Every
producing service takes it and nothing else, so the entire question "did
this service deliver during the request" is answered by the type of its
constructor argument.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.platform.events import DomainEvent
from app.platform.outbox.entry import OutboxEntry


class EventPublisher(Protocol):
    """What a producing application service holds.

    **Enlists in the caller's transaction and never commits.** That is the
    whole of AD-16: the event row is written by the same unit of work that
    writes the state change, so the two are one fact. A publisher that
    committed would reintroduce exactly the two-transaction split the
    pattern exists to remove.

    Consequently `publish` does no I/O a caller can observe failing
    independently — it stages an `INSERT`. If the caller's transaction rolls
    back, the event was never owed and is correctly gone.
    """

    async def publish(self, event: DomainEvent) -> OutboxEntry:
        """Stages one event for publication inside the current transaction.

        Returns the entry so a producer can log the event id it emitted —
        the identifier that ties an HTTP request to a consumer's ledger row
        during an incident.
        """
        ...


class OutboxRepository(Protocol):
    """Storage for outbox entries. The relay's only view of the table.

    Every method is designed for **more than one worker**, because the
    deployment shape AD-17 describes is several relay processes against one
    table. `claim` is the method where that is not free — see its contract.
    """

    async def enqueue(self, entry: OutboxEntry) -> OutboxEntry:
        """Writes one entry. Flushes, never commits (repositories.md §5.1)."""
        ...

    async def claim(
        self, *, limit: int, claimed_by: str, now: datetime, max_attempts: int
    ) -> Sequence[OutboxEntry]:
        """Takes up to `limit` due entries, oldest first, for this worker.

        **The one method that must be safe under concurrency**, and the
        contract is: two relays calling this simultaneously receive disjoint
        sets. An implementation that read and then updated in two statements
        would hand the same event to both, and at-least-once would become
        at-least-twice on every poll rather than only on a crash.

        `max_attempts` is a *parameter* rather than a property of the store,
        so the retry ceiling stays configuration owned by the worker
        (`OutboxSettings`) and the table stays a table.

        Ordered by `occurred_at` — causation order, database.md §12.5 — with
        the id as the tiebreak.
        """
        ...

    async def mark_published(self, entry_ids: Sequence[UUID], *, at: datetime) -> int:
        """Marks entries delivered. Batched: one statement per relay tick.

        Returns the number of rows actually updated, which is the count an
        operator sees in the log line — not `len(entry_ids)`, which would
        report success for a row somebody else had already published.
        """
        ...

    async def mark_failed(self, entry_id: UUID, *, error: str, retry_at: datetime) -> None:
        """Records a delivery failure and schedules the next attempt.

        Does **not** touch `attempt_count`: the claim already counted this
        attempt, so this method records only *why* and *when next*. Keeping
        the counter's owner to one place is what stops a failure path from
        accidentally granting an extra retry — and it is why there is no
        `at` parameter here, since the instant that matters is `retry_at`.
        """
        ...

    async def get(self, entry_id: UUID) -> OutboxEntry | None:
        """One entry by id — for tests and for an operator's investigation.

        On the port rather than only on the adapter because the relay's
        contract tests assert publication state through it, and a test that
        reached into the table directly would pass while the mapper was
        broken.
        """
        ...


class ProcessedEventStore(Protocol):
    """The `(consumer, event_id)` ledger — domain-model.md §13.6.

    Exists because at-least-once delivery is a certainty rather than a risk
    (AD-16). A consumer that is *naturally* idempotent still needs this: the
    dispatcher's effect is a delivery, and delivering the same notification
    twice is visible to a person even though it corrupts nothing.
    """

    async def unprocessed(self, consumer: str, event_ids: Sequence[UUID]) -> frozenset[UUID]:
        """Which of these this consumer has not yet processed.

        **Batched**, and that is not an optimisation: the relay claims a
        page of events per tick, and one `SELECT` per event would make the
        idempotency check itself the N+1 the batch exists to avoid.
        """
        ...

    async def mark_processed(
        self, consumer: str, event_ids: Sequence[UUID], *, at: datetime
    ) -> None:
        """Records that this consumer has handled these events.

        Idempotent: re-recording an existing pair is not an error, because
        the crash window between handling and recording is exactly what
        produces one.
        """
        ...


class OutboxRetentionStore(Protocol):
    """Deleting what the outbox no longer owes anybody — A64-014.1.

    **A fifth protocol rather than three more methods on
    `OutboxRepository`**, and the split is the one every port pair on this
    platform makes: what differs is the capability. The relay can claim,
    publish and fail an entry; it must not be able to *delete* one, because
    a bug in the delivery path that reached a `DELETE` would destroy the
    durable event log AD-17 says projections rebuild from.

    Satisfied by `SqlAlchemyOutboxRetentionStore`, which is constructed only
    by the pruner's own session — nothing on the HTTP path holds it.

    ## Why retention exists at all

    AD-16 makes the outbox as durable as the fact that caused it, and
    A64-013.7 shipped it with rows retained forever. That is correct for
    replay and wrong for capacity: CLAUDE.md §10.5 requires everything
    unbounded to be bounded, and DB-18 already calls this the platform's
    highest-churn relation. An unbounded log is an outage waiting for enough
    traffic.

    ## Why the cutoff is `occurred_at` and not `published_at`

    Because DB-18 makes range partitioning by `occurred_at` the *eventual*
    retention mechanism, and a prune expressed in a different column is one
    that has to be rewritten on the day partitions arrive. Expressed this
    way, `prune_published` and `DETACH PARTITION` select the same rows, so
    the migration replaces an implementation rather than a policy.
    """

    async def prune_published(self, *, before: datetime, batch_size: int) -> int:
        """Deletes up to `batch_size` published entries older than `before`.

        Returns how many rows went. **Never touches an unpublished row**,
        whatever its age — an entry that has exhausted its attempts is
        still owed to somebody, and `OutboxEntry` is explicit that it must
        stay visible in the backlog rather than be tidied away.

        Bounded by `batch_size` and safe for more than one pruner, by the
        same `FOR UPDATE SKIP LOCKED` the relay's claim uses. A retention
        job that took an unbounded `DELETE` lock on the highest-churn
        relation would be an incident of its own.
        """
        ...

    async def prune_processed_events(self, *, before: datetime, batch_size: int) -> int:
        """Deletes up to `batch_size` ledger rows processed before `before`.

        Returns how many rows went. The ledger exists to stop a redelivery
        being re-handled (§13.6), so a row may only be dropped once the
        entry it names can no longer be redelivered — which is why the
        caller prunes the outbox first and holds this horizon at or beyond
        the outbox's. See `RetentionPolicy`.
        """
        ...

    async def unpublished_before(self, instant: datetime) -> int:
        """How many entries older than `instant` are still unpublished.

        Not used to decide anything — it is the number that says *why* an
        old partition could not be detached, which is the question DB-18's
        future `DETACH` raises and nothing else can answer. Served by
        `ix_outbox__unpublished`, whose predicate matches exactly, so it
        counts the backlog rather than the table.
        """
        ...


class EventHandler(Protocol):
    """A consumer, from the relay's point of view.

    **Batch-first.** `handle` takes a sequence rather than one entry, so a
    consumer that needs to read profiles or relationships can do it once for
    the whole tick instead of once per event — the difference between three
    queries and three hundred on a busy platform.

    A handler reports per-entry failures rather than raising, so one poison
    event cannot hold back the twenty beside it. Raising is still legal and
    is treated as "the whole batch failed" — the honest reading of an
    exception nobody classified.
    """

    @property
    def consumer(self) -> str:
        """This consumer's stable name — the ledger's partition key.

        Renaming it re-delivers every retained event to the new name, which
        is a migration and not a rename. It is a property rather than a
        constructor argument so two instances of one consumer cannot be
        wired with two names.
        """
        ...

    def handles(self, event_type: str) -> bool:
        """Whether this consumer wants that event type.

        Asked by the relay per entry, in-process and without I/O. A
        subscription table would be the alternative and is not warranted:
        subscriptions are code, they change with a deploy, and a database
        row that disagreed with the deployed handler would be a silently
        undelivered event.
        """
        ...

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence["EventFailure"]:
        """Processes a batch. Returns the entries that failed, or empty.

        Everything not named in the return value is treated as delivered and
        marked published. A handler that swallowed an error and returned
        nothing would therefore be asserting success — which is why the
        dispatcher's own error path returns failures rather than logging and
        continuing.
        """
        ...


class EventFailure(Protocol):
    """One entry that a handler could not process."""

    @property
    def entry_id(self) -> UUID: ...

    @property
    def reason(self) -> str:
        """A short, **non-sensitive** description for the `last_error`
        column and the log line. An exception type and message, never a
        payload — the row already holds the payload, and the log must not
        (A64-013.7: never log sensitive payloads)."""
        ...
