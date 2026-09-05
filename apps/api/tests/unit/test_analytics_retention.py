"""Retention and erasure — A64-027.2 §47–§53.

Both are deletions, and a deletion is the one operation whose test must be
exact: too little and a policy is not kept, too much and data nobody agreed
to lose is gone.

The clock is frozen. A retention test against a wall clock is a test that
passes for 399 days and fails on the four hundredth.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.modules.analytics.application.services.erasure import AnalyticsErasureService
from app.modules.analytics.application.services.retention import (
    RETENTION_DAYS,
    AnalyticsRetentionService,
)
from app.platform.metrics import NullMetrics

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class _FrozenClock:
    def now(self) -> datetime:
        return NOW


class _RecordingPruner:
    """A store of events by instant, deleting the oldest first."""

    def __init__(self, occurred: list[datetime]) -> None:
        self.remaining = sorted(occurred)
        self.calls: list[tuple[datetime, int]] = []

    async def delete_older_than(self, cutoff: datetime, *, limit: int) -> int:
        self.calls.append((cutoff, limit))
        doomed = [instant for instant in self.remaining if instant < cutoff][:limit]
        for instant in doomed:
            self.remaining.remove(instant)
        return len(doomed)


class TestTheHorizon:
    def test_it_is_four_hundred_days(self) -> None:
        """D2, frozen. 365 would delete the oldest cohort on the day it
        became comparable with itself a year on."""
        assert RETENTION_DAYS == 400

    @pytest.mark.parametrize(
        ("age_days", "survives"),
        [(0, True), (399, True), (400, True), (401, False), (800, False)],
    )
    async def test_the_boundary(self, age_days: int, survives: bool) -> None:
        """**Exactly 400 days old survives.** §49 asks for the boundary to
        be defined rather than discovered, so: the predicate is
        `occurred_at < now - 400 days`, which is strict — a row deleted at
        exactly the horizon would mean "kept for fewer than 400 days" on
        the one day it matters.

        A day either way is not the point; having the answer written down
        is, because a retention boundary nobody stated is one two people
        will implement differently.
        """
        event = NOW - timedelta(days=age_days)
        pruner = _RecordingPruner([event])

        await AnalyticsRetentionService(
            pruner=pruner, clock=_FrozenClock(), metrics=NullMetrics()
        ).prune()

        assert (event in pruner.remaining) is survives


class TestTheRunIsBounded:
    async def test_it_stops_at_its_ceiling_and_says_so(self) -> None:
        """An unbounded delete over a year of events is one long
        transaction holding one long lock, and ingestion blocks behind it."""
        old = [NOW - timedelta(days=500) for _ in range(25)]
        pruner = _RecordingPruner(old)

        result = await AnalyticsRetentionService(
            pruner=pruner,
            clock=_FrozenClock(),
            metrics=NullMetrics(),
            batch_size=5,
            max_batches=2,
        ).prune()

        assert result.deleted == 10
        assert result.exhausted is True
        assert len(pruner.remaining) == 15

    async def test_a_later_run_continues(self) -> None:
        """Resumable by construction rather than by bookkeeping: the cutoff
        is recomputed and the oldest rows are still the oldest."""
        pruner = _RecordingPruner([NOW - timedelta(days=500) for _ in range(8)])
        service = AnalyticsRetentionService(
            pruner=pruner,
            clock=_FrozenClock(),
            metrics=NullMetrics(),
            batch_size=3,
            max_batches=1,
        )

        first = await service.prune()
        second = await service.prune()
        third = await service.prune()

        assert [first.deleted, second.deleted, third.deleted] == [3, 3, 2]
        assert pruner.remaining == []

    async def test_the_cutoff_is_computed_once_per_run(self) -> None:
        """A run that spans midnight must delete one day's worth, not creep
        into the next as it goes."""
        pruner = _RecordingPruner([NOW - timedelta(days=500) for _ in range(6)])

        await AnalyticsRetentionService(
            pruner=pruner,
            clock=_FrozenClock(),
            metrics=NullMetrics(),
            batch_size=2,
            max_batches=5,
        ).prune()

        assert len({cutoff for cutoff, _ in pruner.calls}) == 1

    async def test_an_empty_table_is_one_statement(self) -> None:
        pruner = _RecordingPruner([])

        result = await AnalyticsRetentionService(
            pruner=pruner, clock=_FrozenClock(), metrics=NullMetrics()
        ).prune()

        assert (result.deleted, result.batches, result.exhausted) == (0, 1, False)

    def test_it_refuses_a_nonsense_bound(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            AnalyticsRetentionService(
                pruner=_RecordingPruner([]),
                clock=_FrozenClock(),
                metrics=NullMetrics(),
                batch_size=0,
            )


class TestErasure:
    """**MUTATION D targets these.**"""

    async def test_it_unlinks_and_reports_that_it_did(self) -> None:
        player = uuid4()

        class _Eraser:
            def __init__(self) -> None:
                self.erased: list[UUID] = []

            async def erase(self, player_id: UUID) -> bool:
                self.erased.append(player_id)
                return True

        eraser = _Eraser()
        assert await AnalyticsErasureService(eraser=eraser).erase(player) is True
        assert eraser.erased == [player]

    async def test_erasing_twice_is_not_an_error(self) -> None:
        """A deletion request that failed because there was nothing to
        delete would make the *retry* of a deletion fail."""

        class _Gone:
            async def erase(self, player_id: UUID) -> bool:
                return False

        assert await AnalyticsErasureService(eraser=_Gone()).erase(uuid4()) is False
