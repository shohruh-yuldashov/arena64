"""Replaying finished matches into the projection — A64-020.5F §10, §11.

An **operator command**, never a startup task and never a migration. The
distinction is not ceremony: a rebuild that ran automatically could not be
dry-run, could not be resumed, and would run again on every deploy of a
process that happened to restart.

## Why this is safe to run at any time

It shares everything that matters with the live consumer: the same
`MatchProjectionService`, the same `(match_id, player_id)` claim, the same
`(completed_at, match_id)` ordering. So a match the consumer already
counted is refused by the primary key, and a match the backfill counted is
refused when the event arrives. Running both at once is not a special case
— it is the same case twice.

That is also why it needs no state of its own. "Where did I get to" is
answered by `processed_match`: a restarted run re-scans from the beginning
and skips what is already there. Re-scanning is cheap next to being wrong,
and it removes a checkpoint that could disagree with the ledger.

## Ordering

Oldest first, by `(ended_at, match_id)` — the same total order the
projection compares watermarks with. A backfill that ran newest-first would
produce correct *counts* and a nonsense streak, because every match after
the first would look late.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.modules.game.public import CompletedMatchRecord, CompletedMatchScanner
from app.modules.statistics.application.ports import MatchProjectionUseCase
from app.modules.statistics.application.services.match_projection_service import (
    CompletedMatchFacts,
    ProjectionOutcome,
)
from app.modules.statistics.metrics import STATISTICS_BACKFILL, ProjectionResult
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)

#: How many matches one page reads. Bounded so a rebuild over a large table
#: holds one small transaction at a time rather than one enormous one.
DEFAULT_BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """What one run did — §10's final summary."""

    scanned: int = 0
    applied: int = 0
    already_processed: int = 0
    ignored: int = 0
    failed: int = 0

    def plus(self, **counts: int) -> "BackfillReport":
        return BackfillReport(
            scanned=self.scanned + counts.get("scanned", 0),
            applied=self.applied + counts.get("applied", 0),
            already_processed=self.already_processed + counts.get("already_processed", 0),
            ignored=self.ignored + counts.get("ignored", 0),
            failed=self.failed + counts.get("failed", 0),
        )


class StatisticsBackfill:
    """Folds historical completed matches into player statistics."""

    def __init__(
        self,
        *,
        matches: CompletedMatchScanner,
        projections: MatchProjectionUseCase,
        metrics: MetricsRecorder,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._matches = matches
        self._projections = projections
        self._metrics = metrics
        self._batch_size = batch_size

    async def run(self, *, dry_run: bool = False) -> BackfillReport:
        """Every finished match, oldest first. Never raises.

        `dry_run` scans and reports and **writes nothing** — the answer to
        "what would this do", which an operator should be able to ask
        before doing it. It cannot report `already_processed` accurately,
        because finding that out means attempting the claim; it reports
        what it *would* attempt, and says so.

        One match at a time rather than one transaction per page: each is
        independently durable, so a run stopped between any two matches
        loses nothing and a restart resumes by skipping what is marked.
        """
        report = BackfillReport()
        cursor: tuple[datetime, UUID] | None = None

        while True:
            page = await self._matches.scan_completed(after=cursor, limit=self._batch_size)
            if not page:
                break

            for record in page:
                report = report.plus(scanned=1)
                report = await self._fold(record, report, dry_run=dry_run)

            last = page[-1]
            cursor = (last.completed_at, last.match_id)
            logger.info(
                "statistics_backfill_progress",
                extra={
                    "scanned": report.scanned,
                    "applied": report.applied,
                    "already_processed": report.already_processed,
                    "dry_run": dry_run,
                },
            )

        logger.info(
            "statistics_backfill_completed",
            extra={
                "scanned": report.scanned,
                "applied": report.applied,
                "already_processed": report.already_processed,
                "ignored": report.ignored,
                "failed": report.failed,
                "dry_run": dry_run,
            },
        )
        return report

    async def _fold(
        self, record: CompletedMatchRecord, report: BackfillReport, *, dry_run: bool
    ) -> BackfillReport:
        """One match. A failure is counted and the run continues.

        Continuing is the right posture for a rebuild: stopping at the
        first failure would make one unreadable row block every match after
        it, and the summary names the count so an operator can decide
        whether to investigate or re-run.
        """
        facts = CompletedMatchFacts(
            match_id=record.match_id,
            light_player_id=record.light_player_id,
            dark_player_id=record.dark_player_id,
            outcome=record.outcome.value,
            winner=record.winner.value if record.winner is not None else None,
            completed_at=record.completed_at,
        )

        if dry_run:
            self._metrics.increment(
                STATISTICS_BACKFILL, labels={"result": ProjectionResult.APPLIED}
            )
            return report.plus(applied=1)

        try:
            outcome = await self._projections.apply(facts)
        except Exception as exc:  # noqa: BLE001 — one match must not stop a rebuild
            self._metrics.increment(STATISTICS_BACKFILL, labels={"result": ProjectionResult.FAILED})
            logger.error(
                "statistics_backfill_failed",
                extra={"match_id": str(record.match_id), "error": type(exc).__name__},
                exc_info=exc,
            )
            return report.plus(failed=1)

        self._metrics.increment(STATISTICS_BACKFILL, labels={"result": _RESULTS[outcome]})
        if outcome is ProjectionOutcome.APPLIED:
            return report.plus(applied=1)
        if outcome is ProjectionOutcome.ALREADY_PROCESSED:
            return report.plus(already_processed=1)
        return report.plus(ignored=1)


_RESULTS = {
    ProjectionOutcome.APPLIED: ProjectionResult.APPLIED,
    ProjectionOutcome.ALREADY_PROCESSED: ProjectionResult.ALREADY_PROCESSED,
    ProjectionOutcome.IGNORED_NON_COUNTING: ProjectionResult.IGNORED,
    ProjectionOutcome.REJECTED_INVALID: ProjectionResult.REJECTED,
}


__all__ = ["DEFAULT_BATCH_SIZE", "BackfillReport", "StatisticsBackfill"]
