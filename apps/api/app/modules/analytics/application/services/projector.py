"""The outbox consumer — analytics.md §4, and the ninth on this platform.

Thirty lines of orchestration around `projections.py`, which is where the
translation lives. What this owns is the three things a consumer owns:
which events it wants, what happens when one cannot be read, and the
transaction the write happens in.

## Exactly-once effect, and why it does not depend on the ledger

The relay writes `processed_event` **after** the handler returns and in a
different transaction, so a crash between the two redelivers the batch.
Every consumer on this platform handles that itself; `statistics` claims a
match with `ON CONFLICT DO NOTHING`, and this does the same one level
lower — the analytics row's **primary key is the event id**, so the second
insert conflicts and is ignored.

That makes the effect exactly-once regardless of the ledger, regardless of
which side of the write a crash lands on, and regardless of two workers
racing on the same entry. The ledger remains an optimisation: it stops the
work being done twice, not the row being written twice.

## Failure, and the difference between three of them

    an event analytics ignores      not a failure. The relay hands every
                                    consumer every entry and most belong to
                                    somebody else
    a payload that cannot be read   **skipped, not retried.** A payload
                                    missing a field will still be missing
                                    it next time, and retrying forever
                                    keeps a poison entry at the head of the
                                    backlog — §57's requirement, and the
                                    rule `statistics` already follows
    the database is unavailable     reported, and the relay retries with
                                    backoff

Only the third is a `DeliveryFailure`. The second is counted and logged at
`WARNING`, which is what makes a poison event visible without letting it
block the twenty entries beside it.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.config.environment import Environment
from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.analytics.application.ports import AnalyticsEventStore, SubjectDirectory
from app.modules.analytics.application.services.projections import (
    PROJECTIONS,
    ProjectionError,
    finalise,
    project,
)
from app.modules.analytics.metrics import (
    ANALYTICS_EVENTS_INGESTED,
    ANALYTICS_EVENTS_REJECTED,
    IngestionResult,
    RejectionReason,
)
from app.platform.metrics import MetricsRecorder
from app.platform.outbox import DeliveryFailure, OutboxEntry

logger = logging.getLogger(__name__)

#: The ledger partition. Renaming it redelivers every retained event to the
#: new name, which is a migration rather than a rename.
CONSUMER: Final = "analytics"


class AnalyticsProjector:
    """Projects tracked domain events into the analytics event store."""

    def __init__(
        self,
        *,
        store: AnalyticsEventStore,
        subjects: SubjectDirectory,
        unit_of_work: UnitOfWork,
        clock: Clock,
        environment: Environment,
        metrics: MetricsRecorder,
    ) -> None:
        self._store = store
        self._subjects = subjects
        self._uow = unit_of_work
        self._clock = clock
        self._environment = environment
        self._metrics = metrics

    @property
    def consumer(self) -> str:
        return CONSUMER

    def handles(self, event_type: str) -> bool:
        return event_type in PROJECTIONS

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[DeliveryFailure]:
        """One transaction for the whole batch.

        The subject resolutions and the event inserts commit together, so a
        crash cannot leave a subject created for events that were never
        stored — which would be harmless but would leave a row in the one
        table erasure operates on for somebody who has no history.
        """
        received_at = self._clock.now()

        async with self._uow:
            stored = await self._ingest(entries, received_at)

        self._metrics.increment(
            ANALYTICS_EVENTS_INGESTED,
            labels={"result": IngestionResult.STORED.value},
            by=stored.new,
        )
        if stored.duplicates > 0:
            # Not an error: a redelivery is how at-least-once works.
            # Counted because a *rising* duplicate rate means the relay is
            # retrying something, which is worth seeing before it becomes a
            # backlog.
            self._metrics.increment(
                ANALYTICS_EVENTS_INGESTED,
                labels={"result": IngestionResult.DUPLICATE.value},
                by=stored.duplicates,
            )
        return ()

    async def _ingest(self, entries: Sequence[OutboxEntry], received_at: datetime) -> "Ingested":
        events = []
        for entry in entries:
            try:
                pending = project(entry)
            except ProjectionError as error:
                # Skipped, not reported: see the module docstring. The event
                # is marked handled and will not come back, which is the
                # only way a payload that can never be read stops costing a
                # retry a second forever.
                self._metrics.increment(
                    ANALYTICS_EVENTS_REJECTED,
                    labels={"reason": RejectionReason.UNREADABLE_PAYLOAD.value},
                )
                logger.warning(
                    "analytics_projection_skipped",
                    extra={
                        "event_type": entry.event_type,
                        "event_id": str(entry.id),
                        "error": str(error),
                    },
                )
                continue

            for candidate in pending:
                subject_key = (
                    await self._subjects.resolve(candidate.player_id)
                    if candidate.player_id is not None
                    else None
                )
                events.append(
                    finalise(
                        candidate,
                        subject_key=subject_key,
                        occurred_at=entry.occurred_at,
                        received_at=received_at,
                        environment=self._environment,
                        is_synthetic=False,
                        source_event_id=entry.id,
                    )
                )

        if not events:
            return Ingested(new=0, duplicates=0)

        # A database failure here propagates, so the relay retries the
        # batch — the one failure that is transient and worth another
        # attempt. The relay turns it into per-entry failures itself.
        stored = await self._store.append(events)
        await self._uow.commit()
        return Ingested(new=stored, duplicates=len(events) - stored)


@dataclass(frozen=True, slots=True)
class Ingested:
    """What one batch produced. `duplicates` is the deduplication working."""

    new: int
    duplicates: int
