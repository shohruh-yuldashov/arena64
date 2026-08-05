"""`game.application.ports.ClockDeadlineStore` in memory — A64-020.5A-pre §14.

Records what was scheduled rather than merely accepting it, because the
question this fake exists to answer is *"does activating a timed match write
a deadline"* — a question no assertion about the match record can reach. The
store's own atomicity and its `SKIP LOCKED`-equivalent claim are asserted
against real Redis in `tests/contract/test_live_clock.py`; nothing here
models those.

`fails` makes `schedule` raise, which is the only way to reach the branch
`MatchAcceptanceService._open_the_clock` documents: a deadline that could
not be written must not fail an acceptance that already committed.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from app.modules.engine import PlayerSide
from app.modules.game.application.ports import ClaimedDeadline


class RecordingClockDeadlines:
    """A deadline store that keeps a list."""

    def __init__(self, *, fails: bool = False) -> None:
        self.scheduled: list[tuple[UUID, int, PlayerSide, datetime]] = []
        self.cancelled: list[UUID] = []
        self._fails = fails

    async def schedule(
        self, match_id: UUID, *, ply_number: int, side: PlayerSide, deadline: datetime
    ) -> None:
        if self._fails:
            raise RuntimeError("redis is unreachable")
        self.scheduled.append((match_id, ply_number, side, deadline))

    async def cancel(self, match_id: UUID) -> None:
        self.cancelled.append(match_id)

    async def claim_expired(self, *, now: datetime, limit: int) -> Sequence[ClaimedDeadline]:
        return ()


__all__ = ["RecordingClockDeadlines"]
