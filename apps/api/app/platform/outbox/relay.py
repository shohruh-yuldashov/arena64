"""`OutboxRelay` — one tick of the consumer side of AD-16.

Claim, route, hand to consumers, record, mark. The loop that calls this on
an interval is `OutboxWorker`; keeping the two apart is what makes the
interesting half testable without a timer:

    relay.run_once()   deterministic, returns what it did
    worker             sleeps, calls the above, handles cancellation

## The tick, and where each transaction boundary falls

    1. claim a batch                          transaction A, committed
    2. per handler: filter already-processed
       hand the batch over
       record the ledger rows
       mark published / mark failed           transaction B, committed

**Two transactions, deliberately.** The claim commits on its own so that the
rows are visibly taken by this worker before any handler runs — a second
relay polling mid-handler must skip them, and it can only do that if the
claim is committed. Holding one transaction across the handlers would keep
row locks open for the duration of delivery, which is exactly the "long
transaction holding locks while doing I/O" shape that turns a slow consumer
into a database incident.

The cost is the redelivery window: crash after a handler's effect and before
transaction B commits, and the event is delivered twice. That is at-least-once
(AD-16), it is why `ProcessedEventStore` exists, and it is why every consumer
must be idempotent rather than merely careful.

## Why failures are per-entry and not per-batch

A batch that fails as a unit means one poison event holds back every event
claimed beside it, forever, because they retry together and fail together.
`EventHandler.handle` therefore returns the entries it could not process,
and only those are marked failed. An exception escaping a handler is the
unclassified case and does fail the whole batch — the honest reading of "the
consumer does not know what happened".

## Retry: exponential, capped, jittered by nothing

`base * 2 ** (attempt - 1)`, capped. No jitter, and that is a real decision
rather than an omission: jitter exists to de-synchronise many clients
retrying against one dependency, and here the retries are *rows in one
table* processed by a small number of workers whose polls are already
independent. Adding randomness would make the backoff untestable without
injecting a random source, for a herd that cannot form.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.platform.outbox.entry import OutboxEntry
from app.platform.outbox.isolation import ConsumerPolicies
from app.platform.outbox.ports import EventHandler, OutboxRepository, ProcessedEventStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    """One entry a handler could not process — `ports.EventFailure`.

    `reason` is an exception type and message, and never a payload: the row
    already holds the payload, and A64-013.7 forbids logging it.
    """

    entry_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class RelayTick:
    """What one `run_once` did. Returned rather than logged only, so a test
    asserts on the outcome and the worker logs it once."""

    claimed: int
    published: int
    failed: int
    skipped: int
    """Entries no handler wanted, or that every handler had already
    processed. Marked published — see `run_once` on why that is correct and
    not a silent drop."""

    @property
    def is_idle(self) -> bool:
        return self.claimed == 0


class OutboxRelay:
    """Delivers one batch of outbox entries to its registered consumers.

    Constructed per tick, over a session the worker opened, in the same way
    an application service is constructed per request. It holds ports only:
    the relay is testable against fakes with no database and no clock, which
    is the point of AD-06.
    """

    def __init__(
        self,
        *,
        outbox: OutboxRepository,
        processed: ProcessedEventStore,
        handlers: Sequence[EventHandler],
        unit_of_work: UnitOfWork,
        clock: Clock,
        worker_id: str,
        batch_size: int,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
        policies: ConsumerPolicies | None = None,
    ) -> None:
        self._outbox = outbox
        self._processed = processed
        self._handlers = handlers
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        # A64-020.5F. Guards **this relay's own session** — the one
        # `_claim`, the idempotency filter and the ledger write share.
        #
        # `_dispatch` runs the consumers under `asyncio.gather`, and each
        # `_deliver` opens `self._unit_of_work` twice around it. The
        # handlers themselves are isolated (each opens its own session), but
        # those two blocks were not: N consumers interleaved statements on
        # one `AsyncSession`, which is exactly what `_dispatch`'s own
        # docstring says asyncpg does not permit.
        #
        # It surfaced as `IllegalStateChangeError` from a rollback that
        # could not run, and it was invisible to the suite because the unit
        # tests drive the relay with `NullUnitOfWork`.
        #
        # A lock rather than a session per consumer, because the shared work
        # is two short statements and the concurrency worth having is the
        # *handlers'* — which this does not touch.
        self._session_lock = asyncio.Lock()

        # A64-015.6 §5. Defaulted rather than required, so every existing
        # construction site keeps working and a caller that names no policy
        # still gets a timeout — see `ConsumerPolicies.timeout_for` on why
        # the default must not be "no bound".
        self._policies = policies or ConsumerPolicies.of()

    async def run_once(self) -> RelayTick:
        """Processes at most `batch_size` due entries. Never raises.

        A relay that propagated an exception would stop the worker loop that
        called it, which turns one bad batch into "no event on the platform
        is ever delivered again". Every failure below is recorded on the row
        and reported in the return value instead.
        """
        entries = await self._claim()
        if not entries:
            return RelayTick(claimed=0, published=0, failed=0, skipped=0)

        return await self._record(entries, await self._dispatch(entries))

    async def _dispatch(self, entries: Sequence[OutboxEntry]) -> dict[UUID, str]:
        """Hands the batch to every interested consumer, **concurrently** —
        A64-015.6 §5.

        Sequential delivery made a tick cost the *sum* of its consumers, so a
        slow one delayed every other one's work by its own duration, and
        which consumer suffered was decided by the order of a list literal at
        the composition root. `gather` makes a tick cost the slowest one
        instead.

        Safe because the consumers share nothing **of their own**: each
        opens its own session (`SessionScopedNotificationHandler`), writes
        its own `processed_event` partition, and reports failures per entry.
        Two handlers on one session would interleave statements on one
        connection, which asyncpg does not permit — and is the reason that
        adapter exists.

        What they *do* share is this relay's session, for the idempotency
        filter and the ledger write that bracket every delivery. Those are
        serialised by `_session_lock` — A64-020.5F, which is the defect this
        paragraph described and did not cover.

        `return_exceptions=True` because `_deliver` already converts a
        handler's exception into per-entry failures; anything that still
        escapes is a defect in the relay rather than in a consumer, and one
        such defect must not take the other consumers' results with it.
        """
        wanted = [
            (handler, [entry for entry in entries if handler.handles(entry.event_type)])
            for handler in self._handlers
        ]
        interested = [(handler, batch) for handler, batch in wanted if batch]
        if not interested:
            return {}

        results = await asyncio.gather(
            *(self._deliver_within_budget(handler, batch) for handler, batch in interested),
            return_exceptions=True,
        )

        failures: dict[UUID, str] = {}
        for (handler, batch), result in zip(interested, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "event_dispatch_crashed",
                    extra={"consumer": handler.consumer, "error": type(result).__name__},
                    exc_info=result,
                )
                reported: Sequence[DeliveryFailure] = [
                    DeliveryFailure(entry.id, type(result).__name__) for entry in batch
                ]
            else:
                reported = result

            for failure in reported:
                # First failure wins. A second consumer's error against the
                # same entry does not overwrite the first, so `last_error`
                # names one consumer rather than whichever finished last.
                #
                # Under `gather` "first" is no longer a deterministic
                # *order*, and that is an acceptable loss: the field is a
                # diagnostic hint, the per-consumer log lines above carry the
                # whole picture, and nothing branches on it.
                failures.setdefault(failure.entry_id, failure.reason)

        return failures

    async def _deliver_within_budget(
        self, handler: EventHandler, entries: Sequence[OutboxEntry]
    ) -> Sequence[DeliveryFailure]:
        """One consumer's delivery, bounded by its policy — §5.

        A consumer that exceeds its budget fails **its own** slice: those
        entries are retried, and every other consumer's work in this tick has
        already committed. Before this there was no bound at all, so a
        consumer that hung — not failed, hung — stopped the relay for the
        whole process indefinitely.

        The timeout cancels the handler mid-`await`. That is safe for the
        consumers on this platform because each commits its own transaction
        and reports per-entry failures; a cancelled one has either committed
        its work or rolled it back, and the retry finds the ledger telling it
        which. A consumer that could not tolerate cancellation would need to
        shield its own critical section, which is its business rather than
        the relay's.
        """
        timeout = self._policies.timeout_for(handler.consumer)
        try:
            return await asyncio.wait_for(self._deliver(handler, entries), timeout)
        except TimeoutError:
            # `WARNING` rather than `ERROR`: the entries are retried and the
            # other consumers were unaffected, which is the isolation
            # working. A *sustained* rate here is the alert, and it is
            # visible as this consumer's entries repeatedly failing.
            logger.warning(
                "event_delivery_timed_out",
                extra={
                    "consumer": handler.consumer,
                    "event_count": len(entries),
                    "timeout_seconds": timeout,
                },
            )
            return [DeliveryFailure(entry.id, "delivery_timeout") for entry in entries]

    async def _claim(self) -> Sequence[OutboxEntry]:
        """Transaction A. Committed immediately so the claim is visible to
        every other relay before any handler runs."""
        async with self._unit_of_work:
            entries = await self._outbox.claim(
                limit=self._batch_size,
                claimed_by=self._worker_id,
                now=self._clock.now(),
                max_attempts=self._max_attempts,
            )
            await self._unit_of_work.commit()
        return entries

    async def _deliver(
        self, handler: EventHandler, entries: Sequence[OutboxEntry]
    ) -> Sequence[DeliveryFailure]:
        """Hands one consumer everything it subscribed to in this batch.

        The idempotency filter runs **before** the handler, in one query for
        the whole batch: an event this consumer has already processed is not
        handed over again, which is what turns at-least-once delivery into
        at-most-once *effect*.

        The ledger is written **after** the handler returns, and only for the
        entries it did not report as failed. Writing it first would mark an
        event handled that a crash then prevented from being handled.
        """
        async with self._session_lock, self._unit_of_work:
            fresh_ids = await self._processed.unprocessed(
                handler.consumer, [entry.id for entry in entries]
            )
            await self._unit_of_work.commit()

        fresh = [entry for entry in entries if entry.id in fresh_ids]
        if not fresh:
            return ()

        try:
            reported = await handler.handle(fresh)
        except Exception as error:  # noqa: BLE001 — an unclassified failure is still a failure
            # The whole batch, because the handler did not say which part of
            # it survived. Logged with `exc_info` because unlike a reported
            # failure this one has a stack worth keeping.
            logger.error(
                "event_delivery_failed",
                extra={
                    "consumer": handler.consumer,
                    "event_count": len(fresh),
                    "error": type(error).__name__,
                },
                exc_info=error,
            )
            return [DeliveryFailure(entry.id, type(error).__name__) for entry in fresh]

        failed_ids = {failure.entry_id for failure in reported}
        delivered = [entry.id for entry in fresh if entry.id not in failed_ids]

        # **Shielded from the budget timeout**, and the reason is the whole
        # of `_deliver_within_budget`'s contract: a cancellation here would
        # abandon the shared session mid-statement and leave the *next*
        # consumer's block to fail on it. The handler's own work is already
        # committed at this point, so what is being protected is the record
        # that it happened.
        await asyncio.shield(self._mark(handler, delivered))

        for failure in reported:
            logger.warning(
                "event_delivery_failed",
                extra={
                    "consumer": handler.consumer,
                    "event_id": str(failure.entry_id),
                    "error": failure.reason,
                },
            )

        return [DeliveryFailure(failure.entry_id, failure.reason) for failure in reported]

    async def _mark(self, handler: EventHandler, delivered: Sequence[UUID]) -> None:
        """Writes the ledger for what this consumer handled.

        Its own method so it can be shielded — see the call site.
        """
        async with self._session_lock, self._unit_of_work:
            await self._processed.mark_processed(handler.consumer, delivered, at=self._clock.now())
            await self._unit_of_work.commit()

    async def _record(self, entries: Sequence[OutboxEntry], failures: dict[UUID, str]) -> RelayTick:
        """Transaction B: publishes what succeeded, schedules what did not.

        **An entry no handler wanted is published, not left pending.**
        Leaving it unpublished would make the backlog metric — the one number
        that says whether the relay is healthy — grow forever on events
        nobody subscribes to. The row is retained either way (AD-17), so a
        subscriber added later replays from the table rather than from the
        backlog.

        The cost of that choice is that a node whose build does not know an
        event type **destroys** it for every node that does, and A64-021.2H
        found it doing exactly that. The behaviour is still right; what was
        wrong is that it happened invisibly. See the tick log below.
        """
        at = self._clock.now()
        succeeded = [entry.id for entry in entries if entry.id not in failures]
        subscribed = {
            entry.id
            for entry in entries
            if any(handler.handles(entry.event_type) for handler in self._handlers)
        }
        skipped_types = sorted(
            {entry.event_type for entry in entries if entry.id not in subscribed}
        )

        async with self._unit_of_work:
            published = await self._outbox.mark_published(succeeded, at=at)
            for entry in entries:
                reason = failures.get(entry.id)
                if reason is None:
                    continue
                await self._outbox.mark_failed(
                    entry.id, error=reason, retry_at=self._retry_at(entry, at)
                )
            await self._unit_of_work.commit()

        logger.info(
            "outbox_tick_completed",
            extra={
                "claimed": len(entries),
                "published": published,
                "failed": len(failures),
                # A64-021.2H. **Both fields, and the types are the point.**
                # This docstring has claimed since A64-013.7 that the skip
                # "stays visible", and it was visible nowhere: `skipped` was
                # computed, returned on `RelayTick`, read by nobody, and
                # absent from the only line this tick emits. An entry no
                # handler wants is marked published, so a *stale node* —
                # one whose build predates an event type — claims it,
                # discards it, and leaves no ledger row, no ledger gap
                # anybody counts, and no log line. That is silent,
                # unrecoverable loss, and it is exactly how a missing
                # notification went undiagnosed until a person noticed.
                #
                # The count alone would not have found it: sixteen of the
                # platform's twenty-eight event types have no subscriber at
                # all, `game.move_applied` among them, so a non-zero
                # `skipped` is the *normal* state. What identifies a skew is
                # **which** types were dropped — compared against the
                # build's own subscriptions, a type this node has never
                # heard of is a node running the wrong code.
                #
                # A set rather than one line per entry, for the same reason:
                # the vocabulary is closed and small, and a log line per
                # skipped move would be the hot-path logging CLAUDE.md §8.8
                # forbids.
                "skipped": len(entries) - len(subscribed),
                "skipped_event_types": skipped_types,
                "worker_id": self._worker_id,
            },
        )
        return RelayTick(
            claimed=len(entries),
            published=published,
            failed=len(failures),
            skipped=len([entry for entry in entries if entry.id not in subscribed]),
        )

    def _retry_at(self, entry: OutboxEntry, now: datetime) -> datetime:
        """Exponential backoff from the attempt already counted by the claim.

        `attempt_count` is 1 on the first failure, so the first retry waits
        exactly `retry_base_seconds` rather than twice it — an off-by-one
        that is invisible until somebody is watching a stuck queue.
        """
        exponent = max(entry.attempt_count - 1, 0)
        delay = min(self._retry_base_seconds * (2**exponent), self._retry_max_seconds)

        logger.info(
            "event_delivery_retry_scheduled",
            extra={
                "event_id": str(entry.id),
                "attempt": entry.attempt_count,
                "delay_seconds": delay,
            },
        )
        return now + timedelta(seconds=delay)
