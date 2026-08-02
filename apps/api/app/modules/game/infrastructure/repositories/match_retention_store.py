"""The SQLAlchemy adapter for `application.ports.MatchRetentionStore` —
A64-015.5 §8.

Database-only, and deliberately the narrowest adapter in this module: one
delete and one count. It is a separate class from
`SqlAlchemyMatchRecordRepository` for the reason the port is a separate
protocol — the object that can delete a match must not also be the one that
settles one.

## The predicate is the safety property, not the horizon

    status IN ('cancelled', 'expired') AND settled_at < :before

Both clauses matter and they guard different things. The status list is what
makes an `active` match — one somebody is playing, or will play —
unreachable from this statement **however the horizon is configured**; a
misconfigured retention window can delete too much churn, and it cannot
delete a game.

`settled_at` is non-null exactly for settled matches
(`ck_match__settled_iff_answered`), so a `pending_acceptance` row is
excluded twice over: once by the status list, once by a null cutoff column.
That redundancy is deliberate. A pending match that is *older than the whole
retention horizon* is a reconciliation failure — two players holding an
offer nothing resolved — and deleting it would destroy the evidence instead
of surfacing it. `unsettled_before` is what surfaces it.
"""

import logging
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.infrastructure.models import MatchRecordModel

logger = logging.getLogger(__name__)

#: The two statuses a match reaches without ever being played.
#:
#: Derived from the enum rather than typed out, so a fifth status cannot
#: silently join the set this job deletes — which is the one change to
#: `MatchRecordStatus` that would need a decision rather than a migration.
_ABANDONED = (MatchRecordStatus.CANCELLED, MatchRecordStatus.EXPIRED)


class SqlAlchemyMatchRetentionStore:
    """Constructed only by the retention job's own session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def prune_abandoned(self, *, before: datetime, batch_size: int) -> int:
        """Deletes up to `batch_size` abandoned matches settled before
        `before`. Returns how many rows went.

        `SELECT ... FOR UPDATE SKIP LOCKED` then `DELETE ... WHERE id IN`,
        which is the shape every bounded delete on this platform uses: two
        retention workers running together take disjoint sets instead of
        contending, and the lock is held only for the rows this batch is
        about to remove.

        Ordered by `settled_at` so the oldest churn goes first — a run that
        deleted an arbitrary page would leave the floor of the relation
        ragged, and "how far back does match history go" would stop having
        an answer.
        """
        stale = (
            select(MatchRecordModel.id)
            .where(
                MatchRecordModel.status.in_(_ABANDONED),
                MatchRecordModel.settled_at.is_not(None),
                MatchRecordModel.settled_at < before,
            )
            .order_by(MatchRecordModel.settled_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )

        claimed = list((await self._session.scalars(stale)).all())
        if not claimed:
            return 0

        await self._session.execute(
            delete(MatchRecordModel).where(MatchRecordModel.id.in_(claimed))
        )
        return len(claimed)

    async def unsettled_before(self, instant: datetime) -> int:
        """How many matches older than `instant` are still pending.

        Counted on `created_at`, because a pending match has no
        `settled_at` — which is the same constraint that makes the delete
        above safe, read from the other side.
        No index leads with `created_at`, and none needs to: every partial
        index on this relation is predicated on `pending_acceptance`, so the
        scan
        is over the matches currently awaiting an answer — a handful on a
        healthy platform, and on an unhealthy one exactly the rows this count
        exists to report.
        """
        counted = await self._session.scalar(
            select(func.count())
            .select_from(MatchRecordModel)
            .where(
                MatchRecordModel.status == MatchRecordStatus.PENDING_ACCEPTANCE,
                MatchRecordModel.created_at < instant,
            )
        )
        return int(counted or 0)


__all__ = ["SqlAlchemyMatchRetentionStore"]
