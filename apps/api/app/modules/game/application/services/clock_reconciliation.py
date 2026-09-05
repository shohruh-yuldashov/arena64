"""Re-deriving clock deadlines from the durable match — A64-028.4, P3-4.

    python: ClockDeadlineReconciliationTask
    task:   game.clock.reconcile

## The gap this closes

`ClockAdjudicationService` states it plainly and has since A64-018:

    A deadline that could not be settled because the database was
    unreachable is simply gone, and the match stops flagging until its next
    move writes a new one — which for a game nobody is moving in means it
    stays open. … the correct fix is a sweep that re-derives deadlines from
    active matches.

A64-028.3 then established that `clock:v1:deadlines` does not survive a
Redis loss, and filed the missing backstop as a **P3** about unbounded
growth. That was the wrong severity, and A64-028.4 §21 says so: the growth
is the small half. The real consequence of losing that sorted set is that
**every active game stops being able to flag** — a player who walks away
never loses on time, and their opponent waits for ever on a game that no
process will ever settle. Nothing rebuilt it.

This is that sweep.

## Why it can exist at all

Because the deadline is not information — it is arithmetic over durable
columns. `game.match` holds `clock_light_ms`, `clock_dark_ms`,
`clock_turn_started_at` and `ply_number`, all committed with the move that
set them. So the deadline is derivable, and Redis holds a *cache of a
derivation* in exactly the way `RedisLiveMatchStore` holds a cache of a
replay.

## Why it is safe to run on every instance and on every pass

`RedisClockDeadlineStore.schedule` **supersedes**: one member per match,
replaced whatever it held. So re-deriving a deadline that is already correct
writes the same member with the same score, and two instances doing it at
once converge on the same value. There is nothing to coordinate, which is
why this is not behind a lock (§13's class A).

## What it does not do

It does not adjudicate, and it does not decide that a clock has run out —
`ClockAdjudicationService` owns that and keeps owning it. This only makes
sure the queue it reads is not missing entries the database can prove
belong in it.
"""

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.modules.engine import PlayerSide
from app.modules.game.application.ports import ClockDeadlineStore
from app.platform.metrics import MetricsRecorder
from app.platform.tasks.ports import TaskRequest

logger = logging.getLogger(__name__)

CLOCK_RECONCILE_TASK: Final = "game.clock.reconcile"


def reconcile_request() -> TaskRequest:
    """The scheduled request. No payload: the sweep's input is the database."""
    return TaskRequest(name=CLOCK_RECONCILE_TASK)


#: Active matches with a clock that has started, oldest turn first.
#:
#: `clock_turn_started_at IS NOT NULL` is the seam between "a game that has
#: begun" and one waiting on its first move: a match whose clock has not
#: started has no deadline to derive, and inventing one would flag a player
#: for a turn that never began.
_ACTIVE_CLOCKS = text("""
    SELECT id, ply_number, clock_light_ms, clock_dark_ms, clock_turn_started_at
    FROM game.match
    WHERE status = 'active'
      AND clock_turn_started_at IS NOT NULL
      AND clock_light_ms IS NOT NULL
      AND clock_dark_ms IS NOT NULL
    ORDER BY clock_turn_started_at
    LIMIT :limit
""")


class ClockDeadlineReconciliationTask:
    """`platform.tasks.TaskHandler` — one sweep, over one session."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        deadlines: ClockDeadlineStore,
        clock: Clock,
        metrics: MetricsRecorder,
        batch_size: int = 500,
    ) -> None:
        self._session_factory = session_factory
        self._deadlines = deadlines
        self._clock = clock
        self._metrics = metrics
        self._batch_size = batch_size

    @property
    def name(self) -> str:
        return CLOCK_RECONCILE_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Re-derives every active match's deadline and writes it.

        Read-only against PostgreSQL. The only write is to Redis, and it is
        a supersede — so a pass that finds everything already correct costs
        one query and N idempotent writes, and a pass after a Redis loss
        rebuilds the whole queue.
        """
        restored = 0
        async with self._session_factory() as session:
            rows = (await session.execute(_ACTIVE_CLOCKS, {"limit": self._batch_size})).all()

        for row in rows:
            # Light moves on odd plies, so after ply N the side to move is
            # light when N is even. `game.domain.clock` owns this rule for
            # play; here it is the one fact needed to pick which remaining
            # clock the deadline is measured against.
            side = PlayerSide.LIGHT if row.ply_number % 2 == 0 else PlayerSide.DARK
            remaining_ms = row.clock_light_ms if side is PlayerSide.LIGHT else row.clock_dark_ms
            deadline = row.clock_turn_started_at + timedelta(milliseconds=remaining_ms)

            await self._deadlines.schedule(
                row.id, ply_number=row.ply_number, side=side, deadline=deadline
            )
            restored += 1

        # Bounded label set: a count, never a match id.
        self._metrics.increment("game.clock_deadlines_reconciled_total", by=restored)
        if restored:
            logger.info("clock_deadlines_reconciled", extra={"matches": restored})


__all__ = [
    "CLOCK_RECONCILE_TASK",
    "ClockDeadlineReconciliationTask",
    "reconcile_request",
]
