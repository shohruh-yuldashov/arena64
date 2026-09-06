"""The SQLAlchemy adapters for the outbox's two relations.

Database-only (repositories.md §2). Nothing here decides whether an event
should be delivered, who should receive it, or whether a failure is
retryable — those are the relay's and the consumer's, and a repository that
knew any of them would be the second place each rule lived.

## The claim is the only interesting statement in this file

Everything else is an insert or an update by primary key. `claim` is where
"design for future horizontal workers" (A64-013.7) is either true or a
comment, and the shape that makes it true is:

    SELECT ... FOR UPDATE SKIP LOCKED

`SKIP LOCKED` is what makes N relays *cooperate* instead of *collide*. Two
workers polling simultaneously each take rows the other did not: the second
does not wait on the first's locks (which would serialise them into one
worker with extra latency) and does not read the same rows (which would
deliver everything twice on every tick).

The alternative shapes and why each is worse:

    UPDATE ... RETURNING with no lock hint   one statement, but two workers
                                             block on each other's row
                                             locks — correct and serial
    optimistic version column                a lost race costs a wasted
                                             read plus a retry, per row,
                                             per tick
    advisory lock on the whole table         one worker by construction;
                                             the horizontal scaling AD-17
                                             assumes is then impossible

The claim runs as `SELECT` then `UPDATE` rather than one statement, and both
are inside the caller's transaction: the lock taken by the `SELECT` is held
until that transaction ends, so the window between the two carries no risk.
The relay commits immediately after, which is what publishes the claim to
the other workers.
"""

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.outbox.entry import OutboxBacklog, OutboxEntry
from app.platform.outbox.models import OutboxModel, ProcessedEventModel

logger = logging.getLogger(__name__)

#: How much of a failure reason is kept on the row. Long enough for an
#: exception type and a message, short enough that a driver dumping a query
#: into `str(error)` cannot turn one bad event into a wide row on the
#: platform's highest-churn relation.
_MAX_ERROR_LENGTH = 500


class SqlAlchemyOutboxRepository:
    """Constructed per unit of work with that work's session
    (repositories.md §5.1) — never holds one longer."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: OutboxModel) -> OutboxEntry:
        return OutboxEntry(
            id=row.id,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            event_type=row.event_type,
            event_version=row.event_version,
            payload=row.payload,
            occurred_at=row.occurred_at,
            correlation_id=row.correlation_id,
            causation_id=row.causation_id,
            published_at=row.published_at,
            attempt_count=row.attempt_count,
            next_attempt_at=row.next_attempt_at,
            claimed_at=row.claimed_at,
            claimed_by=row.claimed_by,
            last_error=row.last_error,
        )

    async def enqueue(self, entry: OutboxEntry) -> OutboxEntry:
        """Stages the row inside the caller's transaction.

        **No flush.** Every other repository on this platform flushes so a
        constraint violation surfaces where it can be translated; this one
        has no constraint that can fail — the id is application-generated
        and unique by construction, and there is no uniqueness rule on an
        event. Flushing would buy nothing and would force a round trip into
        the middle of a business transaction that is about to commit anyway.

        The entry is returned unchanged rather than re-read: its id was
        assigned before the insert, which is the property AD-16 needs and
        the reason DB-07 generates ids in the application.
        """
        self._session.add(
            OutboxModel(
                id=entry.id,
                aggregate_type=entry.aggregate_type,
                aggregate_id=entry.aggregate_id,
                event_type=entry.event_type,
                event_version=entry.event_version,
                payload=entry.payload,
                occurred_at=entry.occurred_at,
                correlation_id=entry.correlation_id,
                causation_id=entry.causation_id,
                attempt_count=entry.attempt_count,
                next_attempt_at=entry.next_attempt_at,
            )
        )
        return entry

    async def claim(
        self,
        *,
        limit: int,
        claimed_by: str,
        now: datetime,
        max_attempts: int,
        lease: timedelta,
    ) -> Sequence[OutboxEntry]:
        """Takes up to `limit` due entries for this worker. See the module
        docstring on `SKIP LOCKED`.

        "Due" is three conditions, and each excludes a different row:

            published_at IS NULL          not already delivered
            attempt_count < max_attempts  not exhausted — see `OutboxEntry`
                                          on why exhausted rows stay
                                          unpublished rather than moving
            next_attempt_at <= now        not backing off. Null is due:
                                          a row that has never been tried
                                          has no schedule to respect

        Served by `ix_outbox__unpublished`, whose predicate matches the
        first condition exactly — so the scan touches the backlog and never
        the published majority.
        """
        due = (
            select(OutboxModel.id)
            .where(
                OutboxModel.published_at.is_(None),
                OutboxModel.attempt_count < max_attempts,
                (OutboxModel.next_attempt_at.is_(None)) | (OutboxModel.next_attempt_at <= now),
            )
            .order_by(OutboxModel.occurred_at, OutboxModel.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

        claimed_ids = list((await self._session.scalars(due)).all())
        if not claimed_ids:
            return ()

        # The counter is incremented **here**, at claim time, and not on the
        # failure path — see `OutboxEntry` on why a relay that dies
        # mid-handler must still burn an attempt.
        await self._session.execute(
            update(OutboxModel)
            .where(OutboxModel.id.in_(claimed_ids))
            .values(
                claimed_at=now,
                claimed_by=claimed_by,
                attempt_count=OutboxModel.attempt_count + 1,
                # The lease, and the fix for P2-9. Without it a claimed row
                # satisfied the due predicate again the instant this
                # transaction committed, so a second relay polling a second
                # later claimed the same rows: both delivered, one
                # published, and the other spent an attempt recording
                # nothing because there was nothing left to publish. Five of
                # those retire an event that never failed.
                #
                # `SKIP LOCKED` cannot help — it separates two relays for
                # the length of one statement, and the claim commits
                # immediately afterwards.
                #
                # `mark_published` and `mark_failed` both overwrite this, so
                # the lease governs only a tick that reached neither.
                next_attempt_at=now + lease,
            )
        )

        rows = await self._session.scalars(
            select(OutboxModel)
            .where(OutboxModel.id.in_(claimed_ids))
            .order_by(OutboxModel.occurred_at, OutboxModel.id)
        )
        return [self._to_domain(row) for row in rows]

    async def mark_published(self, entry_ids: Sequence[UUID], *, at: datetime) -> int:
        """One statement for the whole tick, whatever the batch size.

        `published_at IS NULL` in the predicate as well as the id list, so a
        row another worker published between this worker's claim and its
        commit is not silently re-stamped with a later instant — the count
        returned then reflects what this worker actually did.
        """
        if not entry_ids:
            return 0

        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(OutboxModel)
                .where(OutboxModel.id.in_(entry_ids), OutboxModel.published_at.is_(None))
                .values(published_at=at, claimed_at=None, claimed_by=None, last_error=None)
            ),
        )
        return int(result.rowcount)

    async def backlog(self, *, now: datetime, max_attempts: int) -> OutboxBacklog:
        """The three numbers an operator watches — A64-028.6 §3.

        One statement rather than three, because they are read together on
        every scrape and a backlog query that costs three round trips is one
        an operator turns off.

        `oldest_pending_age_seconds` is the number that matters most and is
        the one nothing published before: a backlog of ten thousand that is
        draining is healthy, and a backlog of three whose oldest entry is
        from yesterday is an incident. A count alone cannot tell them apart.

        Exhausted rows are counted separately and **excluded** from the
        retryable backlog: they are not waiting for anything, and including
        them would make a permanent loss look like a growing queue that
        might still drain.
        """
        row = (
            await self._session.execute(
                select(
                    func.count()
                    .filter(OutboxModel.attempt_count < max_attempts)
                    .label("retryable"),
                    func.count()
                    .filter(OutboxModel.attempt_count >= max_attempts)
                    .label("exhausted"),
                    func.min(OutboxModel.occurred_at)
                    .filter(OutboxModel.attempt_count < max_attempts)
                    .label("oldest"),
                ).where(OutboxModel.published_at.is_(None))
            )
        ).one()

        oldest: datetime | None = row.oldest
        return OutboxBacklog(
            retryable=int(row.retryable),
            exhausted=int(row.exhausted),
            oldest_pending_age_seconds=(
                max(0.0, (now - oldest).total_seconds()) if oldest is not None else 0.0
            ),
        )

    async def lock_in_order(self, entry_ids: Sequence[UUID]) -> None:
        """Takes every row lock this tick needs, in ascending id order.

        **Deadlock avoidance, and it is not theoretical** — A64-028.5A §26
        observed `DeadlockDetectedError` on two instances during a
        matchmaking burst, three times per node, which burned the five
        attempts of 809 presence events and abandoned them permanently.

        The cycle: a tick writes its successes as one batched `UPDATE ...
        WHERE id IN (...)` and then its failures one row at a time, so the
        rows of a single transaction are locked partly as a set and partly
        in claim order. Two relays whose claims overlap — which a lease
        that lapses while a slow handler is still running makes ordinary —
        then take the same two locks in opposite orders and PostgreSQL
        breaks the tie by killing one.

        Sorting the *writes* is not enough, because a success and a failure
        are written by different statements and the order between the two
        groups is what differs. Taking every lock first, in one statement,
        in one total order every relay agrees on, removes the cycle by
        construction: two ticks can still contend, but one now simply waits.

        No `SKIP LOCKED` here, deliberately. Skipping is right when
        *choosing* work — it is what lets relays cooperate — and wrong when
        recording work already done: a skipped row would leave a delivered
        event unpublished and it would be delivered again.
        """
        if not entry_ids:
            return
        await self._session.execute(
            select(OutboxModel.id)
            .where(OutboxModel.id.in_(sorted(entry_ids)))
            .order_by(OutboxModel.id)
            .with_for_update()
        )

    async def mark_failed(self, entry_id: UUID, *, error: str, retry_at: datetime) -> None:
        """Records why one entry failed and when it may be tried again.

        Clears `claimed_by` so the row reads as available rather than as
        held by a worker that has moved on — a stale claim is the state an
        operator most easily misreads as "stuck".
        """
        await self._session.execute(
            update(OutboxModel)
            .where(OutboxModel.id == entry_id)
            .values(
                last_error=error[:_MAX_ERROR_LENGTH],
                next_attempt_at=retry_at,
                claimed_at=None,
                claimed_by=None,
            )
        )

    async def get(self, entry_id: UUID) -> OutboxEntry | None:
        row = await self._session.get(OutboxModel, entry_id)
        return self._to_domain(row) if row is not None else None


class SqlAlchemyProcessedEventStore:
    """The `(consumer, event_id)` ledger — domain-model.md §13.6."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def unprocessed(self, consumer: str, event_ids: Sequence[UUID]) -> frozenset[UUID]:
        """Which of these this consumer has not seen. One query per batch.

        Returns the *complement* rather than the seen set, because that is
        what the caller acts on and computing it here keeps the subtraction
        in one place. An empty input short-circuits: the relay routinely
        ticks with nothing to do, and a `WHERE IN ()` is a query issued to
        learn nothing.
        """
        if not event_ids:
            return frozenset()

        seen = await self._session.scalars(
            select(ProcessedEventModel.event_id).where(
                ProcessedEventModel.consumer == consumer,
                ProcessedEventModel.event_id.in_(event_ids),
            )
        )
        already = frozenset(seen.all())
        return frozenset(event_id for event_id in event_ids if event_id not in already)

    async def mark_processed(
        self, consumer: str, event_ids: Sequence[UUID], *, at: datetime
    ) -> None:
        """Records the batch. **`ON CONFLICT DO NOTHING`**, which is the
        whole reason this is safe.

        The crash window between handling an event and recording it is
        exactly what produces a redelivery, so the redelivery's ledger write
        must not be the thing that fails. A plain `INSERT` would raise on
        the primary key and turn a successful second delivery into a
        recorded failure — retried forever, always conflicting.
        """
        if not event_ids:
            return

        statement = pg_insert(ProcessedEventModel).values(
            [
                {"consumer": consumer, "event_id": event_id, "processed_at": at}
                for event_id in event_ids
            ]
        )
        await self._session.execute(
            statement.on_conflict_do_nothing(index_elements=["consumer", "event_id"])
        )


class SqlAlchemyOutboxRetentionStore:
    """The delete side — `ports.OutboxRetentionStore`, A64-014.1.

    A class of its own rather than three methods on
    `SqlAlchemyOutboxRepository`, so that the object the relay holds has no
    way to remove a row. See the port for the argument.

    ## Both prunes are the relay's claim with a different verb

    A64-014.1 requires that nothing invent a second concurrent-claiming
    mechanism, and neither statement below does: each selects its batch with
    `FOR UPDATE SKIP LOCKED` and deletes exactly what it locked. Two pruners
    therefore take disjoint batches instead of blocking on each other, which
    is the same property `claim` has and for the same reason.

    The alternative — a bare `DELETE ... WHERE occurred_at < :cutoff` — is
    one statement and is worse in both directions: it is unbounded, so a
    first run against a year of history takes a lock proportional to the
    backlog on the platform's highest-churn relation, and two pruners
    running it serialise behind each other's row locks.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def prune_published(self, *, before: datetime, batch_size: int) -> int:
        """One bounded batch of expired, delivered entries.

        The predicate is two conditions and dropping either would be a
        defect rather than a widening:

            occurred_at < before        past the retention horizon, and
                                        expressed in the *partition key*
                                        so this and a future `DETACH
                                        PARTITION` select alike (DB-18)
            published_at IS NOT NULL    still owed to nobody. An exhausted
                                        entry is unpublished and stays,
                                        however old — see `OutboxEntry`

        Ordered by `occurred_at` so a backlog drains oldest-first and the
        table's floor rises monotonically, which is what makes the "oldest
        retained row" metric mean anything.
        """
        doomed = (
            select(OutboxModel.id)
            .where(
                OutboxModel.occurred_at < before,
                OutboxModel.published_at.is_not(None),
            )
            .order_by(OutboxModel.occurred_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )

        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(OutboxModel).where(OutboxModel.id.in_(doomed.scalar_subquery()))
            ),
        )
        return int(result.rowcount)

    async def prune_processed_events(self, *, before: datetime, batch_size: int) -> int:
        """One bounded batch of ledger rows whose entries are already gone.

        Matched on the composite primary key rather than on `processed_at`
        directly, so the `DELETE` removes exactly the rows the bounded
        select locked — a second `WHERE processed_at < before` on the delete
        would be unbounded again and the limit would be decorative.
        """
        doomed = (
            select(ProcessedEventModel.consumer, ProcessedEventModel.event_id)
            .where(ProcessedEventModel.processed_at < before)
            .order_by(ProcessedEventModel.processed_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )

        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(ProcessedEventModel).where(
                    tuple_(ProcessedEventModel.consumer, ProcessedEventModel.event_id).in_(doomed)
                )
            ),
        )
        return int(result.rowcount)

    async def unpublished_before(self, instant: datetime) -> int:
        """The rows that are older than the horizon and still owed.

        Served by `ix_outbox__unpublished`, so this counts the backlog and
        never the retained majority — which is what makes it cheap enough to
        run on every prune rather than only when somebody asks.
        """
        count = await self._session.scalar(
            select(func.count())
            .select_from(OutboxModel)
            .where(OutboxModel.published_at.is_(None), OutboxModel.occurred_at < instant)
        )
        return int(count or 0)
