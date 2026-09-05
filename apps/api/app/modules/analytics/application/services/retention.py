"""Raw analytics retention — 400 days, decided in A64-027.2's D2.

## Why the horizon is 400 and not 365

A year-on-year comparison needs both ends of the year to still be there.
365 days means the oldest cohort is deleted on the day it becomes
comparable; 400 leaves five weeks of margin for a query written the week
after somebody thinks of it.

## Bounded, always

The delete is `LIMIT`ed per batch and the run is `LIMIT`ed in batches, for
the reason `OutboxRetentionPolicy` gives: an unbounded `DELETE` over a table
holding a year of events is one long transaction holding one long lock, and
the first symptom is ingestion blocking behind the cleanup that was supposed
to be invisible.

A run that hits its batch ceiling stops and says so. The next run continues,
because the cutoff is recomputed from the clock and the oldest rows are
still the oldest — the job is **idempotent and resumable** by construction
rather than by bookkeeping.

## `occurred_at`, not `received_at`

The retention clock is the fact's own instant. `received_at` would mean an
event delayed by a relay backlog outlives one that arrived on time, so two
events describing the same day would be deleted on different days — and a
daily aggregate computed after the first deletion and before the second
would be wrong in a way nobody could reproduce.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from app.core.clock import Clock
from app.modules.analytics.application.ports import RetentionPruner
from app.modules.analytics.metrics import ANALYTICS_RETENTION_DELETED
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)

#: D2, frozen. A setting rather than a constant would invite a deployment to
#: quietly keep more than the policy allows, which is the direction that
#: matters — a shorter window is a product decision, a longer one is a
#: privacy one.
RETENTION_DAYS: Final = 400


@dataclass(frozen=True, slots=True)
class PruneResult:
    deleted: int
    batches: int
    exhausted: bool
    """Whether the run stopped at its ceiling with rows still to delete.

    Reported rather than inferred: a run that is always exhausted means the
    ceiling is below the arrival rate, and the table grows despite a prune
    that looks like it is working.
    """


class AnalyticsRetentionService:
    """Deletes raw events older than the horizon, in bounded batches."""

    def __init__(
        self,
        *,
        pruner: RetentionPruner,
        clock: Clock,
        metrics: MetricsRecorder,
        batch_size: int = 5_000,
        max_batches: int = 20,
    ) -> None:
        if batch_size < 1 or max_batches < 1:
            raise ValueError("batch_size and max_batches must be positive")
        self._pruner = pruner
        self._clock = clock
        self._metrics = metrics
        self._batch_size = batch_size
        self._max_batches = max_batches

    async def prune(self) -> PruneResult:
        """One run. Never raises for a row it could not delete.

        The cutoff is computed **once** per run rather than per batch, so a
        run that spans midnight deletes one day's worth rather than
        creeping into the next as it goes.
        """
        cutoff = self._clock.now() - timedelta(days=RETENTION_DAYS)
        deleted = 0
        batches = 0

        while batches < self._max_batches:
            removed = await self._pruner.delete_older_than(cutoff, limit=self._batch_size)
            batches += 1
            deleted += removed
            if removed < self._batch_size:
                # Fewer than asked for means there were no more. Stopping
                # here rather than on zero saves one round trip per run and
                # is the same condition.
                self._metrics.increment(ANALYTICS_RETENTION_DELETED, by=deleted)
                logger.info(
                    "analytics_retention_pruned",
                    extra={"deleted": deleted, "batches": batches, "cutoff": cutoff.isoformat()},
                )
                return PruneResult(deleted=deleted, batches=batches, exhausted=False)

        self._metrics.increment(ANALYTICS_RETENTION_DELETED, by=deleted)
        logger.warning(
            "analytics_retention_ceiling_reached",
            extra={"deleted": deleted, "batches": batches, "cutoff": cutoff.isoformat()},
        )
        return PruneResult(deleted=deleted, batches=batches, exhausted=True)
