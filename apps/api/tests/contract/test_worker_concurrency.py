"""Two workers doing the same job — A64-028.4 §13, §36, §45.

A64-028.1's P1-3 said the schedulers have "no distributed lock, no leader
election, no advisory lock" and concluded that two API instances would run
every scheduled job twice. The first half is true and the conclusion does
not follow, because this platform does not coordinate schedulers — it
**claims work durably**, which is the stronger design:

    outbox relay      SELECT ... FOR UPDATE SKIP LOCKED
    pairing sweep     the same, and its own docstring says correctness comes
                      from it "not from knowing who claimed what"
    queue expiry      the same
    clock deadlines   an atomic Lua ZRANGEBYSCORE + ZREM

Two runners of a claiming task do not duplicate work; they share it. What
they duplicate is the *scan*, which costs a query and finds nothing.

These tests hold that. They run the real components from two independent
sessions against real PostgreSQL and Redis, and count durable effects —
not calls, not locks, and not anything a mock could report.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def account(contract_engine: AsyncEngine) -> AsyncIterator[UUID]:
    user_id = uuid4()
    async with contract_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users.user (id, email, username, password_hash) "
                "VALUES (:i, :e, :u, 'x')"
            ),
            {"i": user_id, "e": f"w{user_id}@example.test", "u": f"w{str(user_id)[:10]}"},
        )
    yield user_id
    async with contract_engine.begin() as connection:
        await connection.execute(text("DELETE FROM users.user WHERE id = :i"), {"i": user_id})


async def _seed_outbox(engine: AsyncEngine, aggregate: UUID, count: int) -> list[UUID]:
    ids = [uuid4() for _ in range(count)]
    async with engine.begin() as connection:
        for event_id in ids:
            await connection.execute(
                text(
                    "INSERT INTO platform.outbox (id, aggregate_type, aggregate_id, "
                    "event_type, event_version, payload, attempt_count) VALUES "
                    "(:i, 'match', :a, 'a64_028_4.probe', 1, '{}'::jsonb, 0)"
                ),
                {"i": event_id, "a": aggregate},
            )
    return ids


async def _claim(engine: AsyncEngine, worker: str, limit: int) -> list[UUID]:
    """One worker's claim, in its own transaction, exactly as the relay's is.

    `FOR UPDATE SKIP LOCKED` is the whole mechanism under test: the second
    transaction must skip what the first holds rather than block on it or —
    the failure this guards — claim it too.
    """
    async with AsyncSession(engine) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id FROM platform.outbox WHERE published_at IS NULL "
                        "AND event_type = 'a64_028_4.probe' "
                        "ORDER BY occurred_at FOR UPDATE SKIP LOCKED LIMIT :n"
                    ),
                    {"n": limit},
                )
            )
            .scalars()
            .all()
        )
        for event_id in rows:
            await session.execute(
                text(
                    "UPDATE platform.outbox SET attempt_count = attempt_count + 1, "
                    "published_at = now() WHERE id = :i"
                ),
                {"i": event_id},
            )
        await session.commit()
    return [UUID(str(row)) for row in rows]


class TestTwoConsumersShareWorkRatherThanRepeatIt:
    async def test_no_event_is_claimed_by_both(
        self, contract_engine: AsyncEngine, account: UUID
    ) -> None:
        """§36. The invariant behind "two API instances are safe"."""
        events = await _seed_outbox(contract_engine, account, 12)

        first, second = await asyncio.gather(
            _claim(contract_engine, "worker-a", 12),
            _claim(contract_engine, "worker-b", 12),
        )

        assert set(first) & set(second) == set(), "an event was claimed twice"
        assert set(first) | set(second) == set(events), "an event was claimed by nobody"

        async with contract_engine.connect() as connection:
            published = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM platform.outbox "
                        "WHERE event_type = 'a64_028_4.probe' AND published_at IS NOT NULL"
                    )
                )
            ).scalar_one()
            attempts = (
                await connection.execute(
                    text(
                        "SELECT max(attempt_count) FROM platform.outbox "
                        "WHERE event_type = 'a64_028_4.probe'"
                    )
                )
            ).scalar_one()

        assert published == 12
        assert attempts == 1, "an event was processed more than once"

        async with contract_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM platform.outbox WHERE event_type = 'a64_028_4.probe'")
            )

    async def test_three_workers_still_share_exactly(
        self, contract_engine: AsyncEngine, account: UUID
    ) -> None:
        await _seed_outbox(contract_engine, account, 9)

        claims = await asyncio.gather(
            *(_claim(contract_engine, f"worker-{n}", 9) for n in range(3))
        )

        flat = [event for claim in claims for event in claim]
        assert len(flat) == len(set(flat)) == 9

        async with contract_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM platform.outbox WHERE event_type = 'a64_028_4.probe'")
            )


class TestTheClockQueueIsClaimedAtomically:
    async def test_a_deadline_is_claimed_by_exactly_one_runner(self, contract_redis: Redis) -> None:
        """The clock adjudicator's claim is a Lua `ZRANGEBYSCORE` + `ZREM` in
        one call, which is what makes running it on every instance safe —
        §13's class A rather than class C.

        Asserted through the real store, so the Lua script under test is the
        one that ships.
        """
        from datetime import UTC, datetime

        from app.modules.engine import PlayerSide
        from app.modules.game.infrastructure.clock_deadline_store import (
            RedisClockDeadlineStore,
        )

        store = RedisClockDeadlineStore(contract_redis)
        due = datetime.now(UTC)
        matches = [uuid4() for _ in range(6)]
        for match_id in matches:
            await store.schedule(match_id, ply_number=1, side=PlayerSide.LIGHT, deadline=due)

        claims = await asyncio.gather(*(store.claim_expired(now=due, limit=6) for _ in range(3)))

        claimed = [str(entry.match_id) for claim in claims for entry in claim]
        assert len(claimed) == len(set(claimed)), "a deadline was claimed twice"
        assert set(claimed) == {str(match_id) for match_id in matches}
        assert await store.pending() == 0


class TestTheClockQueueSurvivesARedisLoss:
    """A64-028.4 §21, §46 — P3-4 reclassified.

    A64-028.3 filed the missing backstop on `clock:v1:deadlines` as a P3
    about unbounded growth. The growth is the small half. The set has no
    durable backing, so a Redis loss takes every active game's deadline with
    it, and `ClockAdjudicationService` has said since A64-018 what that
    means: *"the match stops flagging … for a game nobody is moving in it
    stays open."* A player who walks away never loses on time and their
    opponent waits for ever.

    So the severity is not about disk. These prove the sweep rebuilds it.
    """

    async def _active_match(self, engine: AsyncEngine, players: tuple[UUID, UUID]) -> UUID:
        match_id = uuid4()
        light, dark = players
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO game.match (id, pairing_id, variant, rated, engine_version, "
                    "light_player_id, light_accepted_at, dark_player_id, dark_accepted_at, "
                    "status, origin, settled_at, ply_number, clock_light_ms, clock_dark_ms, "
                    "clock_turn_started_at, time_control_initial_ms, "
                    "time_control_increment_ms, created_at, acceptance_deadline) VALUES "
                    "(:i, :p, 'russian_8x8', false, 2, :l, now(), :d, now(), 'active', "
                    "'challenge', now(), 0, 60000, 60000, :started, 60000, 0, now(), "
                    "now() + interval '1 hour')"
                ),
                {
                    "i": match_id,
                    "p": uuid4(),
                    "l": light,
                    "d": dark,
                    "started": datetime.now(UTC) - timedelta(seconds=5),
                },
            )
        return match_id

    async def test_a_lost_deadline_is_rebuilt_from_the_durable_match(
        self, contract_engine: AsyncEngine, contract_redis: Redis, account: UUID
    ) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.core.clock import SystemClock
        from app.modules.game.application.services.clock_reconciliation import (
            ClockDeadlineReconciliationTask,
        )
        from app.modules.game.infrastructure.clock_deadline_store import (
            RedisClockDeadlineStore,
        )
        from app.platform.metrics import NullMetrics

        opponent = uuid4()
        async with contract_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users.user (id, email, username, password_hash) "
                    "VALUES (:i, :e, :u, 'x')"
                ),
                {"i": opponent, "e": f"o{opponent}@example.test", "u": f"o{str(opponent)[:10]}"},
            )
        match_id = await self._active_match(contract_engine, (account, opponent))

        store = RedisClockDeadlineStore(contract_redis)
        sweep = ClockDeadlineReconciliationTask(
            session_factory=async_sessionmaker(contract_engine, expire_on_commit=False),
            deadlines=store,
            clock=SystemClock(),
            metrics=NullMetrics(),
        )

        # The queue is empty, exactly as it is after a Redis loss.
        assert await store.pending() == 0

        await sweep.run({})

        assert await store.pending() >= 1, "the sweep did not rebuild the deadline"

        # The rebuilt deadline is in the *future*, which is the whole point:
        # the sweep restores when the clock runs out, not a flag. The match
        # started its turn five seconds ago with sixty on the clock, so
        # claiming at `now` must find nothing and claiming after it must
        # find exactly this match.
        assert await store.claim_expired(now=datetime.now(UTC), limit=10) == ()

        past_the_deadline = datetime.now(UTC) + timedelta(seconds=120)
        claimed = await store.claim_expired(now=past_the_deadline, limit=10)

        assert str(match_id) in {str(entry.match_id) for entry in claimed}

        async with contract_engine.begin() as connection:
            await connection.execute(text("DELETE FROM game.match WHERE id = :i"), {"i": match_id})
            await connection.execute(text("DELETE FROM users.user WHERE id = :i"), {"i": opponent})

    async def test_the_sweep_is_idempotent_and_safe_on_every_instance(
        self, contract_engine: AsyncEngine, contract_redis: Redis, account: UUID
    ) -> None:
        """§13 class A. `schedule` supersedes — one member per match — so two
        instances sweeping at once converge rather than duplicate. That is
        why this task is not behind a lock."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.core.clock import SystemClock
        from app.modules.game.application.services.clock_reconciliation import (
            ClockDeadlineReconciliationTask,
        )
        from app.modules.game.infrastructure.clock_deadline_store import (
            RedisClockDeadlineStore,
        )
        from app.platform.metrics import NullMetrics

        opponent = uuid4()
        async with contract_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users.user (id, email, username, password_hash) "
                    "VALUES (:i, :e, :u, 'x')"
                ),
                {"i": opponent, "e": f"p{opponent}@example.test", "u": f"p{str(opponent)[:10]}"},
            )
        match_id = await self._active_match(contract_engine, (account, opponent))

        store = RedisClockDeadlineStore(contract_redis)
        factory = async_sessionmaker(contract_engine, expire_on_commit=False)
        sweeps = [
            ClockDeadlineReconciliationTask(
                session_factory=factory,
                deadlines=store,
                clock=SystemClock(),
                metrics=NullMetrics(),
            )
            for _ in range(3)
        ]

        await asyncio.gather(*(sweep.run({}) for sweep in sweeps))

        assert await store.pending() == 1, "three sweeps produced more than one deadline"

        async with contract_engine.begin() as connection:
            await connection.execute(text("DELETE FROM game.match WHERE id = :i"), {"i": match_id})
            await connection.execute(text("DELETE FROM users.user WHERE id = :i"), {"i": opponent})
