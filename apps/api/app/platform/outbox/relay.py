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

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.platform.outbox.entry import OutboxEntry
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

        failures: dict[UUID, str] = {}
        for handler in self._handlers:
            wanted = [entry for entry in entries if handler.handles(entry.event_type)]
            if not wanted:
                continue
            for failure in await self._deliver(handler, wanted):
                # First failure wins. A second handler's error against the
                # same entry does not overwrite the first, so `last_error`
                # names the consumer that failed *first* — which is the one
                # an operator should look at.
                failures.setdefault(failure.entry_id, failure.reason)

        return await self._record(entries, failures)

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
        async with self._unit_of_work:
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

        async with self._unit_of_work:
            await self._processed.mark_processed(handler.consumer, delivered, at=self._clock.now())
            await self._unit_of_work.commit()

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

    async def _record(self, entries: Sequence[OutboxEntry], failures: dict[UUID, str]) -> RelayTick:
        """Transaction B: publishes what succeeded, schedules what did not.

        **An entry no handler wanted is published, not left pending.** It is
        counted separately so the distinction stays visible, but leaving it
        unpublished would make the backlog metric — the one number that says
        whether the relay is healthy — grow forever on events nobody
        subscribes to. The row is retained either way (AD-17), so a
        subscriber added later replays from the table rather than from the
        backlog.
        """
        at = self._clock.now()
        succeeded = [entry.id for entry in entries if entry.id not in failures]
        subscribed = {
            entry.id
            for entry in entries
            if any(handler.handles(entry.event_type) for handler in self._handlers)
        }

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
