"""Operator command for the statistics projection — A64-020.5F §10.

    python -m app.operator.statistics backfill --dry-run
    python -m app.operator.statistics backfill

One command. It replays every finished match through the **same**
projection the live consumer uses, so a rebuild and live consumption
produce the same rows by construction rather than by agreement.

See `app/operator/__init__.py` for why this is a process profile rather
than an `/api/v1/admin` route. It matters more here than for the
tournament commands: a rebuild reachable over HTTP is a rebuild an
authenticated request can trigger, and §26 requires it not be exposed.

## Why it is safe to run at any time

Nothing about it is special-cased. It shares the claim
(`processed_match`'s primary key), the rules (`MatchProjectionService`) and
the ordering (`(ended_at, match_id)`) with the live consumer, so:

    run it twice          the second run reports every match as
                          `already_processed` and writes nothing
    stop it halfway       restart re-scans from the beginning and skips
                          what is marked. There is no checkpoint to
                          corrupt, because the ledger *is* the checkpoint
    run it while live     a match the consumer counted is refused here and
                          vice versa — the same collision, from either side

It never truncates and never resets. §10 forbids it, and the reason is
that a rebuild which cleared first would have a window in which the
platform's totals were zero.

## Dry run

`--dry-run` scans and reports and writes nothing. It cannot distinguish
"would apply" from "already processed" — finding that out means attempting
the claim — so it reports everything it *would attempt* as `applied` and
says so in the summary line.

## Exit codes

`0` when every match was scanned, `1` when any failed. A failure is
counted rather than fatal, so one unreadable row does not block the rest
— and the exit code is what tells a script that something needs looking
at.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from app.common.logging import configure_logging
from app.config.settings import get_settings
from app.core.clock import SystemClock
from app.database.session_manager import DatabaseSessionManager
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.infrastructure.repositories import SqlAlchemyMatchHistoryRepository
from app.modules.statistics.application.services.backfill_service import (
    DEFAULT_BATCH_SIZE,
    BackfillReport,
    StatisticsBackfill,
)
from app.modules.statistics.application.services.match_projection_service import (
    MatchProjectionService,
)
from app.modules.statistics.infrastructure.repositories.statistics_repository import (
    SqlAlchemyStatisticsRepository,
)
from app.platform.metrics import process_metrics

logger = logging.getLogger(__name__)


async def backfill(*, dry_run: bool, batch_size: int) -> BackfillReport:
    """Replays every finished match. Returns what it did.

    One session for the whole run, and the projection commits per match
    inside it — so a run stopped at any point has durably counted every
    match it reported.
    """
    settings = get_settings()
    database = DatabaseSessionManager(settings.postgres)
    try:
        async with database.session_factory() as session:
            projection = MatchProjectionService(
                statistics=SqlAlchemyStatisticsRepository(session),
                unit_of_work=SessionUnitOfWork(session),
                clock=SystemClock(),
            )
            return await StatisticsBackfill(
                matches=SqlAlchemyMatchHistoryRepository(session),
                projections=projection,
                metrics=process_metrics(),
                batch_size=batch_size,
            ).run(dry_run=dry_run)
    finally:
        await database.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.operator.statistics",
        description="Rebuild player statistics from finished matches.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("backfill", help="Fold every finished match into statistics.")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report without writing anything.",
    )
    run.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Matches read per page (default {DEFAULT_BATCH_SIZE}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(level=settings.app.log_level, environment=settings.environment)
    arguments = _parser().parse_args(argv)

    report = asyncio.run(backfill(dry_run=arguments.dry_run, batch_size=arguments.batch_size))

    summary = (
        f"scanned={report.scanned} applied={report.applied} "
        f"already_processed={report.already_processed} "
        f"ignored={report.ignored} failed={report.failed}"
    )
    if arguments.dry_run:
        summary += " (dry run — nothing was written; `applied` is what would be attempted)"

    print(summary)  # noqa: T201 — an operator's terminal
    if report.failed:
        print(f"{report.failed} match(es) failed; see the log", file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["backfill", "main"]
