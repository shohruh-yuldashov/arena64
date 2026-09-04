"""The scheduled half of retention — A64-027.2 §47.

The platform already has a periodic-task mechanism (`platform/tasks`) and an
almost identical job beside this one (`OutboxRetentionTask`), so this reuses
both rather than introducing a scheduler of its own. A second scheduling
mechanism would be a second place for an interval to be wrong.

Owns a session **factory** rather than a session, for the reason
`OutboxRetentionTask` gives: a prune is a no-op on most runs, and a session
held between them holds a connection idle for an hour.
"""

from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.analytics.application.services.retention import AnalyticsRetentionService
from app.modules.analytics.infrastructure.repositories.analytics_repository import (
    SqlAlchemyRetentionPruner,
)
from app.platform.metrics import MetricsRecorder
from app.platform.tasks import TaskRequest

ANALYTICS_PRUNE_TASK: Final = "analytics.events.prune"

#: The same queue the outbox prune uses — an SLO class, not a priority.
#: Deleting last year's rows may wait behind anything a person is waiting on.
MAINTENANCE_QUEUE: Final = "maintenance"


def prune_request() -> TaskRequest:
    """The request that asks for one prune.

    Empty payload: the horizon is a constant and the instant is the
    handler's clock. A request carrying a cutoff would let a stale schedule
    dispatch yesterday's horizon — a way to delete more than the policy
    allows, from the one job that deletes anything.
    """
    return TaskRequest(name=ANALYTICS_PRUNE_TASK, queue=MAINTENANCE_QUEUE)


class AnalyticsRetentionTask:
    """`platform.tasks.TaskHandler` — one prune, over one session."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        metrics: MetricsRecorder,
        batch_size: int = 5_000,
        max_batches: int = 20,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._metrics = metrics
        self._batch_size = batch_size
        self._max_batches = max_batches

    @property
    def name(self) -> str:
        return ANALYTICS_PRUNE_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload — see `prune_request`.

        Commits **per batch** rather than per run: the point of batching is
        that no single transaction holds a lock over a large delete, and one
        commit at the end would undo that.
        """
        async with self._session_factory() as session:
            unit_of_work = SessionUnitOfWork(session)
            service = AnalyticsRetentionService(
                pruner=_CommittingPruner(
                    SqlAlchemyRetentionPruner(session), unit_of_work=unit_of_work
                ),
                clock=self._clock,
                metrics=self._metrics,
                batch_size=self._batch_size,
                max_batches=self._max_batches,
            )
            await service.prune()


class _CommittingPruner:
    """Commits after each batch.

    A decorator rather than a commit inside the repository, because a
    repository that commits is one a caller cannot compose into a larger
    transaction — the rule every repository on this platform follows. The
    batching is this job's concern, so the commit is too.
    """

    def __init__(
        self, inner: SqlAlchemyRetentionPruner, *, unit_of_work: SessionUnitOfWork
    ) -> None:
        self._inner = inner
        self._uow = unit_of_work

    async def delete_older_than(self, cutoff: Any, *, limit: int) -> int:
        async with self._uow:
            deleted = await self._inner.delete_older_than(cutoff, limit=limit)
            await self._uow.commit()
        return deleted
