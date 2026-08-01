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
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.outbox.entry import OutboxEntry
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
        self, *, limit: int, claimed_by: str, now: datetime, max_attempts: int
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
