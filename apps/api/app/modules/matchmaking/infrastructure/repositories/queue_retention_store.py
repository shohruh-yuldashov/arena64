"""The SQLAlchemy adapter for `application.ports.QueueRetentionStore` —
A64-015.5 §8.

Database-only, and deliberately the **narrowest** adapter in this module:
one delete and one count. It is a separate class from
`SqlAlchemyQueueRepository` for the reason `SqlAlchemyOutboxRetentionStore`
is separate from `SqlAlchemyOutboxRepository` — the queue's use cases must
not be able to reach a `DELETE`, and a capability that is not on the object
cannot be reached by a bug in the object.

## The predicate is the safety property, not the horizon

    resolved_at IS NOT NULL AND resolved_at < :before

`resolved_at` is non-null **exactly** for terminal tickets — that is
`ck_queue_ticket__resolved_iff_terminal`, enforced by the database — so the
first clause is "this ticket is finished" expressed as a column test rather
than as a status list somebody has to keep in step with `QueueStatus`.

The consequence worth stating: a `waiting` or `reserved` ticket is
unreachable from this statement **however the horizon is configured**. A
misconfigured retention window can delete too much history; it cannot delete
a player out of the queue, and it cannot delete the stranded reservation
reconciliation is about to recover. That is the guarantee §8 asks for, and
it is held by a schema constraint rather than by this file being careful.
"""

import logging
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.matchmaking.infrastructure.models import QueueTicketModel

logger = logging.getLogger(__name__)


class SqlAlchemyQueueRetentionStore:
    """Constructed only by the retention job's own session — nothing on the
    HTTP path holds one."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def prune_resolved(self, *, before: datetime, batch_size: int) -> int:
        """Deletes up to `batch_size` terminal tickets resolved before
        `before`. Returns how many rows went.

        `SELECT ... FOR UPDATE SKIP LOCKED` then `DELETE ... WHERE id IN`,
        which is the shape the outbox's pruner uses and the reason is the
        same: two retention workers running together take disjoint sets
        instead of contending, and the lock is held only for the rows this
        batch is about to remove.

        Ordered by `resolved_at` so the oldest history goes first. That is
        not cosmetic — a run that deleted an arbitrary page would leave the
        floor of the relation ragged, and "how far back does queue history
        go" would stop having an answer.

        Served by `ix_queue_ticket__retention`, whose predicate matches the
        first clause exactly, so the scan is over finished tickets rather
        than over the table.
        """
        stale = (
            select(QueueTicketModel.id)
            .where(
                QueueTicketModel.resolved_at.is_not(None),
                QueueTicketModel.resolved_at < before,
            )
            .order_by(QueueTicketModel.resolved_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )

        claimed = list((await self._session.scalars(stale)).all())
        if not claimed:
            return 0

        await self._session.execute(
            delete(QueueTicketModel).where(QueueTicketModel.id.in_(claimed))
        )
        return len(claimed)

    async def live_before(self, instant: datetime) -> int:
        """How many live tickets are older than `instant`.

        Not used to decide anything, and on this relation it is a genuine
        alarm rather than bookkeeping: a `waiting` ticket older than the
        whole retention horizon means the expiry sweep has stopped, and a
        `reserved` one that old means reconciliation has. Both are silent
        failures that this count makes loud.

        Counted on `entered_at` rather than `resolved_at`, because a live
        ticket has no `resolved_at` — which is the same constraint that
        makes the delete above safe, read from the other side.
        """
        counted = await self._session.scalar(
            select(func.count())
            .select_from(QueueTicketModel)
            .where(
                QueueTicketModel.resolved_at.is_(None),
                QueueTicketModel.entered_at < instant,
            )
        )
        return int(counted or 0)


__all__ = ["SqlAlchemyQueueRetentionStore"]
