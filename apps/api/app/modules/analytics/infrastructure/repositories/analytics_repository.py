"""The SQLAlchemy side of the four ports.

One session, held rather than opened, so a caller decides the transaction —
the platform's rule for every repository, and here it is what lets the
projector write events and mark the ledger inside one unit of work.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.domain.event import AnalyticsEvent
from app.modules.analytics.domain.subject import SubjectKey
from app.modules.analytics.infrastructure.models import (
    AnalyticsEventModel,
    AnalyticsSubjectModel,
)

logger = logging.getLogger(__name__)


class SqlAlchemyAnalyticsEventStore:
    """`INSERT ... ON CONFLICT DO NOTHING`, and that is the whole design.

    The alternative — read, decide, write — is a race with itself: two
    workers checking for the same event id both find nothing and both
    insert. The conflict clause pushes the decision into the one place that
    can make it atomically.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, events: Sequence[AnalyticsEvent]) -> int:
        if not events:
            return 0

        statement = (
            pg_insert(AnalyticsEventModel)
            .values([_row(event) for event in events])
            .on_conflict_do_nothing(index_elements=[AnalyticsEventModel.id])
            .returning(AnalyticsEventModel.id)
        )
        inserted = (await self._session.execute(statement)).scalars().all()
        return len(inserted)


class SqlAlchemySubjectDirectory:
    """The player-to-subject map.

    `resolve` is an upsert that returns the surviving key rather than a
    select-then-insert, for the reason above: two first events for one
    player arriving together must not create two subjects. `DO UPDATE SET
    player_id = EXCLUDED.player_id` is a no-op write whose only purpose is
    to make the row visible to `RETURNING` on the conflicting path —
    `DO NOTHING` returns nothing at all, which would leave the caller
    unable to tell "already there" from "failed".
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, player_id: UUID) -> SubjectKey:
        statement = (
            pg_insert(AnalyticsSubjectModel)
            .values(player_id=player_id, subject_key=uuid4())
            .on_conflict_do_update(
                index_elements=[AnalyticsSubjectModel.player_id],
                set_={"player_id": player_id},
            )
            .returning(AnalyticsSubjectModel.subject_key)
        )
        key = (await self._session.execute(statement)).scalar_one()
        return SubjectKey(key)

    async def lookup(self, player_id: UUID) -> SubjectKey | None:
        statement = select(AnalyticsSubjectModel.subject_key).where(
            AnalyticsSubjectModel.player_id == player_id
        )
        key = (await self._session.execute(statement)).scalar_one_or_none()
        return SubjectKey(key) if key is not None else None

    async def is_synthetic(self, player_id: UUID) -> bool:
        statement = select(AnalyticsSubjectModel.is_synthetic).where(
            AnalyticsSubjectModel.player_id == player_id
        )
        flag = (await self._session.execute(statement)).scalar_one_or_none()
        return bool(flag)

    async def mark_synthetic(self, player_id: UUID, *, is_synthetic: bool) -> None:
        statement = (
            pg_insert(AnalyticsSubjectModel)
            .values(player_id=player_id, subject_key=uuid4(), is_synthetic=is_synthetic)
            .on_conflict_do_update(
                index_elements=[AnalyticsSubjectModel.player_id],
                set_={"is_synthetic": is_synthetic},
            )
        )
        await self._session.execute(statement)


class SqlAlchemySubjectEraser:
    """Erasure, which is one `DELETE` and nothing else.

    No update of the events, no rewriting of history, no tombstone. The
    events already hold a random key that names nobody; what made it name
    somebody was this row, and deleting it is the erasure.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def erase(self, player_id: UUID) -> bool:
        statement = (
            delete(AnalyticsSubjectModel)
            .where(AnalyticsSubjectModel.player_id == player_id)
            .returning(AnalyticsSubjectModel.player_id)
        )
        deleted = (await self._session.execute(statement)).scalar_one_or_none()
        return deleted is not None


class SqlAlchemyRetentionPruner:
    """The bounded delete.

    A subquery with `LIMIT` rather than `DELETE ... LIMIT`, which PostgreSQL
    does not have. The inner select is covered by
    `ix_analytics_event__occurred_at`, so the prune reads the oldest rows
    rather than scanning the table to find them — which is the difference
    between a job that finishes and a job that is a nightly incident.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def delete_older_than(self, cutoff: datetime, *, limit: int) -> int:
        doomed = (
            select(AnalyticsEventModel.id)
            .where(AnalyticsEventModel.occurred_at < cutoff)
            .order_by(AnalyticsEventModel.occurred_at)
            .limit(limit)
            .scalar_subquery()
        )
        statement = (
            delete(AnalyticsEventModel)
            .where(AnalyticsEventModel.id.in_(doomed))
            .returning(AnalyticsEventModel.id)
        )
        deleted = (await self._session.execute(statement)).scalars().all()
        return len(deleted)


def _row(event: AnalyticsEvent) -> dict[str, object]:
    return {
        "id": event.event_id,
        "event_name": event.event_name.value,
        "event_version": event.event_version,
        "occurred_at": event.occurred_at,
        "received_at": event.received_at,
        "source": event.source,
        "environment": event.environment.value,
        "subject_key": event.subject_key,
        "anonymous_id": event.anonymous_id,
        "session_id": event.session_id,
        "is_synthetic": event.is_synthetic,
        "properties": event.properties,
        "source_event_id": event.source_event_id,
    }
