"""The statistics projection against real PostgreSQL — A64-020.5F §29.

The exactly-once guarantee is a **primary key**, and a primary key cannot
be exercised against a fake. So the real `MatchProjectionService`, the real
repository, the real `processed_match` table and real transactions run
here; what is substituted is nothing at all.

Five tests for §29's items 1–5 and 7. Each carries the assertions belonging
to one *mechanism* — the counters, the draw, the non-counting outcome, the
claim, and the backfill's overlap with live consumption — because §29 caps
the whole phase at twelve across backend and frontend.
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.database.unit_of_work import SessionUnitOfWork

# The **domain** outcome, not `game.public`'s metric-label enum of the
# same name — `CompletedMatchRecord.outcome` is typed with this one.
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.public import CompletedMatchRecord, PlayerSide
from app.modules.statistics.application.services.backfill_service import StatisticsBackfill
from app.modules.statistics.application.services.match_projection_service import (
    CompletedMatchFacts,
    MatchProjectionService,
    ProjectionOutcome,
)
from app.modules.statistics.infrastructure.models import (
    PlayerStatisticsModel,
    ProcessedMatchModel,
)
from app.modules.statistics.infrastructure.repositories.statistics_repository import (
    SqlAlchemyStatisticsRepository,
)
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _service(session: AsyncSession) -> MatchProjectionService:
    """The real service over the real repository and a real transaction."""
    return MatchProjectionService(
        statistics=SqlAlchemyStatisticsRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=MovableClock(NOW),
    )


def _facts(
    *,
    light: UUID,
    dark: UUID,
    outcome: str = "win",
    winner: str | None = "light",
    at: datetime = NOW,
    match_id: UUID | None = None,
) -> CompletedMatchFacts:
    return CompletedMatchFacts(
        match_id=match_id or uuid4(),
        light_player_id=light,
        dark_player_id=dark,
        outcome=outcome,
        winner=winner,
        completed_at=at,
    )


@pytest_asyncio.fixture
async def players(contract_session: AsyncSession) -> tuple[UUID, UUID]:
    """Two ids with no rows. A projection creates them on first count."""
    return uuid4(), uuid4()


async def _counters(session: AsyncSession, player_id: UUID) -> PlayerStatisticsModel | None:
    return await session.get(PlayerStatisticsModel, player_id)


class TestCounting:
    async def test_a_win_credits_both_seats_and_moves_both_streaks(
        self, contract_session: AsyncSession, players: tuple[UUID, UUID]
    ) -> None:
        """§29.1 — one match, two players, opposite results.

        Asserted from the **database** rather than the return value: a
        service reporting a count it did not write would pass every
        assertion made on what it returned.

        The streaks are the interesting half. A win after a win is `+2`; a
        loss after a loss is `-2`; and `best_win_streak` tracks the peak
        rather than the current run, so the loser's stays at zero while
        their current goes negative.
        """
        light, dark = players
        service = _service(contract_session)

        first = await service.apply(_facts(light=light, dark=dark, at=NOW))
        second = await service.apply(_facts(light=light, dark=dark, at=NOW + timedelta(minutes=1)))
        assert first is ProjectionOutcome.APPLIED
        assert second is ProjectionOutcome.APPLIED

        winner = await _counters(contract_session, light)
        loser = await _counters(contract_session, dark)
        assert winner is not None and loser is not None

        assert (winner.games_played, winner.wins, winner.losses, winner.draws) == (2, 2, 0, 0)
        assert (loser.games_played, loser.wins, loser.losses, loser.draws) == (2, 0, 2, 0)
        assert winner.current_streak == 2
        assert winner.best_win_streak == 2
        assert loser.current_streak == -2
        # A losing run says nothing about the best winning one.
        assert loser.best_win_streak == 0

        # The watermark advanced to the later match, so the next in-order
        # match compares against the real high-water mark.
        assert winner.counted_at is not None
        assert winner.counted_match_id is not None

    async def test_a_draw_credits_both_and_resets_both_streaks(
        self, contract_session: AsyncSession, players: tuple[UUID, UUID]
    ) -> None:
        """§29.2. A draw is the one result that moves both players the same
        way — and the only one that returns a streak to zero, which is what
        `current_streak == 0` means rather than "no history"."""
        light, dark = players
        service = _service(contract_session)

        await service.apply(_facts(light=light, dark=dark, at=NOW))
        await service.apply(
            _facts(
                light=light,
                dark=dark,
                outcome="draw",
                winner=None,
                at=NOW + timedelta(minutes=1),
            )
        )

        for player_id in (light, dark):
            row = await _counters(contract_session, player_id)
            assert row is not None
            assert row.games_played == 2
            assert row.draws == 1
            assert row.current_streak == 0

        # The winner keeps the peak the draw interrupted.
        winner = await _counters(contract_session, light)
        assert winner is not None
        assert winner.best_win_streak == 1

    async def test_an_aborted_match_counts_for_nobody(
        self, contract_session: AsyncSession, players: tuple[UUID, UUID]
    ) -> None:
        """§29.3 and §6 — MT-11's "a match that did not happen".

        `MatchOutcome.NONE` is an abort. Counting it as a draw would put a
        game on two permanent records that neither player played, and
        counting it as anything else would need a winner it does not have.

        The absence of a **row** is the assertion, not a count of zero: a
        player whose only match was aborted has no statistics yet, which is
        a legitimate state for a projection (DM-03).
        """
        light, dark = players
        outcome = await _service(contract_session).apply(
            _facts(light=light, dark=dark, outcome="none", winner=None)
        )

        assert outcome is ProjectionOutcome.IGNORED_NON_COUNTING
        assert await _counters(contract_session, light) is None
        assert await _counters(contract_session, dark) is None
        assert (
            await contract_session.scalar(select(func.count()).select_from(ProcessedMatchModel))
        ) == 0


class TestExactlyOnce:
    async def test_the_same_match_delivered_twice_counts_once(
        self, contract_session: AsyncSession, players: tuple[UUID, UUID]
    ) -> None:
        """§29.4 — a duplicate delivery, a relay retry, a redelivery after a
        restart. All the same thing to this: a claim that does not insert.

        Asserted three ways, because the counters alone would pass if the
        second call had silently done nothing *and* left no marker — and
        the marker is what makes the third delivery cheap.
        """
        light, dark = players
        service = _service(contract_session)
        facts = _facts(light=light, dark=dark)

        assert await service.apply(facts) is ProjectionOutcome.APPLIED
        assert await service.apply(facts) is ProjectionOutcome.ALREADY_PROCESSED
        assert await service.apply(facts) is ProjectionOutcome.ALREADY_PROCESSED

        winner = await _counters(contract_session, light)
        assert winner is not None
        assert winner.games_played == 1
        assert winner.wins == 1

        # Two markers — one per player — and no more, whatever the delivery
        # count. The primary key is what refuses the rest.
        markers = await contract_session.scalar(
            select(func.count()).select_from(ProcessedMatchModel)
        )
        assert markers == 2

    async def test_two_concurrent_deliveries_of_one_match_count_once(
        self, contract_engine: AsyncEngine, players: tuple[UUID, UUID]
    ) -> None:
        """§29.4's harder half, and §9's requirement.

        Two **separate sessions** applying the same match at the same time —
        two relay processes, or a backfill racing live consumption. A
        check-then-update would let both read "not processed" and both
        increment; the primary key means one insert wins and the other's
        transaction finds the row.

        Real concurrency: two connections, `asyncio.gather`, no
        synchronisation between them. One raises or reports already
        processed, and the counters land on exactly one game either way —
        which is the only assertion that matters.
        """
        light, dark = players
        facts = _facts(light=light, dark=dark)

        async def deliver() -> None:
            async with AsyncSession(contract_engine, expire_on_commit=False) as session:
                try:
                    await _service(session).apply(facts)
                except Exception:  # noqa: BLE001 — a loser may raise; see below
                    # A unique-violation on the losing side is a legitimate
                    # outcome of a genuine race, and the relay retries it —
                    # at which point the claim finds the row and reports
                    # `already_processed`. What must never happen is two
                    # increments, which is asserted below.
                    await session.rollback()

        await asyncio.gather(deliver(), deliver())

        async with AsyncSession(contract_engine, expire_on_commit=False) as session:
            winner = await session.get(PlayerStatisticsModel, light)
            assert winner is not None
            assert winner.games_played == 1
            assert winner.wins == 1
            markers = await session.scalar(select(func.count()).select_from(ProcessedMatchModel))
            assert markers == 2


class _Scanner:
    """A `CompletedMatchScanner` over a fixed list, keyset-paged.

    The real scanner is `SqlAlchemyMatchHistoryRepository`'s and is
    exercised by the backfill against real rows elsewhere; what this test
    needs is control over *which* matches exist so the overlap with live
    consumption is deterministic.
    """

    def __init__(self, records: Sequence[CompletedMatchRecord]) -> None:
        self._records = sorted(records, key=lambda r: (r.completed_at, r.match_id))

    async def scan_completed(
        self, *, after: tuple[datetime, UUID] | None, limit: int
    ) -> Sequence[CompletedMatchRecord]:
        remaining = [
            record
            for record in self._records
            if after is None or (record.completed_at, record.match_id) > after
        ]
        return remaining[:limit]


class TestBackfill:
    async def test_a_backfill_over_live_counted_history_double_counts_nothing(
        self, contract_session: AsyncSession, players: tuple[UUID, UUID]
    ) -> None:
        """§29.5 and §29.7 — the overlap, and restartability.

        The scenario is the real one: a match is counted **live**, then an
        operator runs a backfill over the whole table. Without a shared
        identity the backfill would count it again; with
        `(match_id, player_id)` it is refused, and the run says so.

        Then the backfill is run a second time, which is what a resumed run
        looks like from the database's point of view — everything already
        marked. Both runs report, and the counters move exactly once.
        """
        light, dark = players
        service = _service(contract_session)

        live = _facts(light=light, dark=dark, at=NOW)
        historical = _facts(
            light=light, dark=dark, outcome="draw", winner=None, at=NOW - timedelta(hours=1)
        )

        # Counted live first — as the consumer would.
        assert await service.apply(live) is ProjectionOutcome.APPLIED

        scanner = _Scanner(
            [
                CompletedMatchRecord(
                    match_id=historical.match_id,
                    light_player_id=light,
                    dark_player_id=dark,
                    outcome=MatchOutcome.DRAW,
                    winner=None,
                    rated=True,
                    termination_reason=TerminationReason.AGREED_DRAW,
                    completed_at=historical.completed_at,
                ),
                CompletedMatchRecord(
                    match_id=live.match_id,
                    light_player_id=light,
                    dark_player_id=dark,
                    outcome=MatchOutcome.WIN,
                    winner=PlayerSide.LIGHT,
                    rated=True,
                    termination_reason=TerminationReason.RESIGNATION,
                    completed_at=live.completed_at,
                ),
            ]
        )
        backfill = StatisticsBackfill(
            matches=scanner, projections=service, metrics=RecordingMetrics(), batch_size=1
        )

        first = await backfill.run()
        assert first.scanned == 2
        # The historical one was new; the live one was already counted.
        assert first.applied == 1
        assert first.already_processed == 1

        # A resumed run: everything is marked, nothing is applied.
        second = await backfill.run()
        assert second.scanned == 2
        assert second.applied == 0
        assert second.already_processed == 2

        winner = await _counters(contract_session, light)
        assert winner is not None
        assert winner.games_played == 2
        assert winner.wins == 1
        assert winner.draws == 1
        # The **live** match is the later one, so it owns the streak — the
        # historical draw was folded in behind the watermark and left it
        # alone. A backfill that rewrote the streak from an older game is
        # exactly what the watermark exists to prevent.
        assert winner.current_streak == 1
