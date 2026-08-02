"""The SQLAlchemy adapters for the two audit surfaces A64-015.6 added.

Database-only (repositories.md §2): whether a player *deserves* a cooldown is
`MatchOutcomeService`'s question and what recovery *did* is
`PairingReconciliationService`'s, and what is left here is an idempotent
insert, two bounded reads and a bounded delete each.

## Both writes are `ON CONFLICT DO NOTHING` followed by a read

Their callers are outbox consumers under AD-16's at-least-once contract, so a
redelivered event reaches them twice **by design** — the ledger stops most of
it and cannot stop two relays delivering concurrently. A check-then-insert
would pass for both and produce two rows, which for an audit trail means two
different answers to one question.

`DO NOTHING` then re-read is the shape that resolves the race in the database.
It costs one extra `SELECT` on the duplicate path, which is the rare one.

## Neither has an `update`

Not an omission. An audit row that could be amended answers "what does the
platform say now" rather than "what happened", and the second is the only
question either relation exists for. The single mutation on both is the
retention delete.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.matchmaking.domain.cooldown_audit import CooldownRecord
from app.modules.matchmaking.domain.reconciliation_timeline import ReconciliationEntry
from app.modules.matchmaking.infrastructure.models import (
    QueueCooldownAuditModel,
    ReconciliationTimelineModel,
)

logger = logging.getLogger(__name__)


class SqlAlchemyCooldownAuditRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1) — never holds a session longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: QueueCooldownAuditModel) -> CooldownRecord:
        return CooldownRecord(
            id=row.id,
            player_id=row.player_id,
            reason=row.reason,
            source_match_id=row.source_match_id,
            applied_at=row.applied_at,
            expires_at=row.expires_at,
            extended_existing=row.extended_existing,
        )

    async def record(self, entry: CooldownRecord) -> CooldownRecord:
        """Writes one audit row, or returns the one already there.

        The conflict target is `uq_queue_cooldown_audit__source`, expressed as
        the columns plus its predicate — PostgreSQL matches a partial unique
        index only when the statement names the same `WHERE`, and omitting it
        raises "no unique or exclusion constraint matching" rather than
        silently inserting a duplicate.

        **Flushes, never commits.** The caller's unit of work spans this row
        and the enforcement row it describes: one transaction, because a bar
        with no record of why is exactly what this port exists to prevent.
        """
        statement = (
            insert(QueueCooldownAuditModel)
            .values(
                id=entry.id,
                player_id=entry.player_id,
                reason=entry.reason,
                source_match_id=entry.source_match_id,
                applied_at=entry.applied_at,
                expires_at=entry.expires_at,
                extended_existing=entry.extended_existing,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    QueueCooldownAuditModel.player_id,
                    QueueCooldownAuditModel.source_match_id,
                ],
                index_where=QueueCooldownAuditModel.source_match_id.is_not(None),
            )
            .returning(QueueCooldownAuditModel)
        )

        written = (await self._session.scalars(statement)).one_or_none()
        await self._session.flush()
        if written is not None:
            return self._to_domain(written)

        # `DO NOTHING` returned no row, so an earlier delivery won. Re-read
        # it: the caller wants what is *recorded*, and after a duplicate that
        # is the first attempt's row rather than the one it just built.
        existing = await self._session.scalar(
            select(QueueCooldownAuditModel).where(
                QueueCooldownAuditModel.player_id == entry.player_id,
                QueueCooldownAuditModel.source_match_id == entry.source_match_id,
            )
        )
        if existing is None:  # pragma: no cover — a conflict implies a row
            # Unreachable: `DO NOTHING` only declines when the index matched.
            # Returning the unstored value would be a caller believing an
            # audit row exists when none does, so this fails loudly instead.
            raise RuntimeError("cooldown audit conflicted with a row that does not exist")

        logger.debug(
            "cooldown_audit_deduplicated",
            extra={"player_id": str(entry.player_id), "record_id": str(existing.id)},
        )
        return self._to_domain(existing)

    async def history_for(self, player_id: UUID, *, limit: int) -> Sequence[CooldownRecord]:
        """This player's cooldowns, most recent first.

        Served by `ix_queue_cooldown_audit__player`, whose column order
        matches this `ORDER BY` — so PostgreSQL walks the index and stops at
        the limit rather than sorting a history.
        """
        rows = await self._session.scalars(
            select(QueueCooldownAuditModel)
            .where(QueueCooldownAuditModel.player_id == player_id)
            .order_by(QueueCooldownAuditModel.applied_at.desc())
            .limit(limit)
        )
        return [self._to_domain(row) for row in rows]

    async def prune_recorded(self, *, before: datetime, batch_size: int) -> int:
        """Deletes audit rows applied before `before`. Returns how many went.

        `SELECT ... FOR UPDATE SKIP LOCKED` then delete by key, the shape
        every bounded delete on this platform uses: two pruners running
        together take disjoint sets instead of contending.
        """
        stale = (
            select(QueueCooldownAuditModel.id)
            .where(QueueCooldownAuditModel.applied_at < before)
            .order_by(QueueCooldownAuditModel.applied_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )

        claimed = list((await self._session.scalars(stale)).all())
        if not claimed:
            return 0

        await self._session.execute(
            delete(QueueCooldownAuditModel).where(QueueCooldownAuditModel.id.in_(claimed))
        )
        return len(claimed)


class SqlAlchemyReconciliationTimelineRepository:
    """The recovery timeline, over one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: ReconciliationTimelineModel) -> ReconciliationEntry:
        return ReconciliationEntry(
            id=row.id,
            event_id=row.event_id,
            ticket_id=row.ticket_id,
            player_id=row.player_id,
            action=row.action,
            match_id=row.match_id,
            pairing_id=row.pairing_id,
            occurred_at=row.occurred_at,
            recorded_at=row.recorded_at,
        )

    async def append(self, entry: ReconciliationEntry) -> ReconciliationEntry:
        """Writes one entry, or returns the one already there for its event.

        The conflict target is `uq_pairing_timeline__event` — a full unique
        index, so no `index_where` is needed and none is given.
        """
        statement = (
            insert(ReconciliationTimelineModel)
            .values(
                id=entry.id,
                event_id=entry.event_id,
                ticket_id=entry.ticket_id,
                player_id=entry.player_id,
                action=entry.action,
                match_id=entry.match_id,
                pairing_id=entry.pairing_id,
                occurred_at=entry.occurred_at,
                recorded_at=entry.recorded_at,
            )
            .on_conflict_do_nothing(index_elements=[ReconciliationTimelineModel.event_id])
            .returning(ReconciliationTimelineModel)
        )

        written = (await self._session.scalars(statement)).one_or_none()
        await self._session.flush()
        if written is not None:
            return self._to_domain(written)

        existing = await self._session.scalar(
            select(ReconciliationTimelineModel).where(
                ReconciliationTimelineModel.event_id == entry.event_id
            )
        )
        if existing is None:  # pragma: no cover — a conflict implies a row
            raise RuntimeError("timeline entry conflicted with a row that does not exist")
        return self._to_domain(existing)

    async def for_ticket(self, ticket_id: UUID, *, limit: int) -> Sequence[ReconciliationEntry]:
        """Everything recovery did to one queue ticket, most recent first."""
        rows = await self._session.scalars(
            select(ReconciliationTimelineModel)
            .where(ReconciliationTimelineModel.ticket_id == ticket_id)
            .order_by(ReconciliationTimelineModel.occurred_at.desc())
            .limit(limit)
        )
        return [self._to_domain(row) for row in rows]

    async def for_pairing(self, pairing_id: UUID, *, limit: int) -> Sequence[ReconciliationEntry]:
        """Everything recovery did to one pairing, most recent first.

        Empty today for every input — `pairing_id` is nullable and nothing
        populates it, because `PairingReconciled` identifies a ticket. See
        `ReconciliationEntry.pairing_id`; the query is written now so the
        caller does not change when the event grows the field.
        """
        rows = await self._session.scalars(
            select(ReconciliationTimelineModel)
            .where(ReconciliationTimelineModel.pairing_id == pairing_id)
            .order_by(ReconciliationTimelineModel.occurred_at.desc())
            .limit(limit)
        )
        return [self._to_domain(row) for row in rows]

    async def prune_recorded(self, *, before: datetime, batch_size: int) -> int:
        """Deletes entries that occurred before `before`. Returns how many
        went."""
        stale = (
            select(ReconciliationTimelineModel.id)
            .where(ReconciliationTimelineModel.occurred_at < before)
            .order_by(ReconciliationTimelineModel.occurred_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )

        claimed = list((await self._session.scalars(stale)).all())
        if not claimed:
            return 0

        await self._session.execute(
            delete(ReconciliationTimelineModel).where(ReconciliationTimelineModel.id.in_(claimed))
        )
        return len(claimed)


__all__ = [
    "SqlAlchemyCooldownAuditRepository",
    "SqlAlchemyReconciliationTimelineRepository",
]
