"""The presence sweeper — A64-013.8's one behavioural change.

A64-013.7 left a gap it recorded honestly: a player who closes the tab
produces no `PresenceOffline`, because nothing observes a key expiring. This
file is the proof that the gap is closed, and it is written against the
**whole** mechanism — the real `RedisPresenceProvider` maintaining the real
roster over `FakePresenceRedis`, and the real `PresenceSweeper` reading it.

Substituting either would make this suite assert that a stub agrees with
itself. What is faked is Redis, and the fake's sorted set is deliberately
*not* expiry-aware — which is exactly the property that makes a sweeper
necessary: the roster outlives the record.

    TestRosterMaintenance  who ends up in the roster, and who leaves it
    TestSweeping           the missing transitions, emitted
    TestTheRaceItAvoids    a player who came back must not be announced gone
    TestFailureBehaviour   a background job that must never escalate
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from redis.asyncio import Redis

from app.config.settings import PresenceSettings
from app.modules.notifications.application.services import PresenceSweeper
from app.modules.users.domain.presence import LapsedPresence
from app.modules.users.infrastructure.presence import RedisPresenceProvider
from app.modules.users.infrastructure.presence.keys import roster_key
from app.platform.outbox import OutboxEntry, OutboxEventPublisher
from tests.fakes.outbox import InMemoryOutbox, NullUnitOfWork
from tests.fakes.presence_redis import (
    NOW,
    FakePresenceRedis,
    MovableClock,
    UnreachablePresenceRedis,
)

TTL_SECONDS = 60
ALICE = UUID("019fbd90-0001-7000-8000-000000000001")
BOB = UUID("019fbd90-0002-7000-8000-000000000002")
CAROL = UUID("019fbd90-0003-7000-8000-000000000003")

OFFLINE = "users.presence_offline"


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def redis(clock: MovableClock) -> FakePresenceRedis:
    return FakePresenceRedis(clock)


@pytest.fixture
def presence(redis: FakePresenceRedis, clock: MovableClock) -> RedisPresenceProvider:
    return RedisPresenceProvider(
        cast(Redis, redis),
        settings=PresenceSettings(ttl_seconds=TTL_SECONDS),
        clock=clock,
    )


@pytest.fixture
def outbox() -> InMemoryOutbox:
    return InMemoryOutbox()


@pytest.fixture
def sweeper(
    presence: RedisPresenceProvider, outbox: InMemoryOutbox, clock: MovableClock
) -> PresenceSweeper:
    return PresenceSweeper(
        # The **same object** on both ports, exactly as the composition root
        # wires it: the sweeper's "is this player back?" re-check is only
        # meaningful if it reads the store the roster describes.
        roster=presence,
        presence=presence,
        events=OutboxEventPublisher(cast(Any, outbox)),
        unit_of_work=cast(Any, NullUnitOfWork()),
        clock=clock,
        batch_size=100,
    )


def _emitted(outbox: InMemoryOutbox) -> list[OutboxEntry]:
    return list(outbox.entries.values())


class TestRosterMaintenance:
    async def test_going_online_puts_a_player_in_the_roster(
        self, presence: RedisPresenceProvider, redis: FakePresenceRedis
    ) -> None:
        """The record alone cannot be swept — an expired key is gone. The
        roster is the thing that outlives the lapse."""
        await presence.record_presence(ALICE, is_online=True)

        assert str(ALICE) in redis.roster(roster_key())

    async def test_the_score_is_the_deadline_and_not_the_observation(
        self, presence: RedisPresenceProvider, redis: FakePresenceRedis, clock: MovableClock
    ) -> None:
        """`ZRANGEBYSCORE 0 now` only means "these windows have closed" if
        the score is when the window closes."""
        await presence.record_presence(ALICE, is_online=True)

        expected_ms = int((clock.now() + timedelta(seconds=TTL_SECONDS)).timestamp() * 1000)
        assert redis.roster(roster_key())[str(ALICE)] == expected_ms

    async def test_a_refresh_moves_the_deadline_rather_than_adding_a_member(
        self, presence: RedisPresenceProvider, redis: FakePresenceRedis, clock: MovableClock
    ) -> None:
        """Otherwise a player refreshing all day would leave a trail of stale
        deadlines, each of which the sweeper would announce."""
        await presence.record_presence(ALICE, is_online=True)
        first = redis.roster(roster_key())[str(ALICE)]

        clock.advance(30)
        await presence.record_presence(ALICE, is_online=True)

        roster = redis.roster(roster_key())
        assert len(roster) == 1
        assert roster[str(ALICE)] > first

    async def test_an_explicit_offline_removes_the_member(
        self, presence: RedisPresenceProvider, redis: FakePresenceRedis
    ) -> None:
        """A player who signed out was already announced by
        `PresenceNotificationService`. Leaving them in the roster would have
        the sweeper announce it a second time when their deadline passed."""
        await presence.record_presence(ALICE, is_online=True)

        await presence.record_presence(ALICE, is_online=False)

        assert redis.roster(roster_key()) == {}


class TestSweeping:
    async def test_a_lapsed_player_produces_an_offline_event(
        self,
        presence: RedisPresenceProvider,
        sweeper: PresenceSweeper,
        outbox: InMemoryOutbox,
        clock: MovableClock,
    ) -> None:
        """The gap, closed. Nothing observed this player leaving — their
        record simply stopped being true."""
        await presence.record_presence(ALICE, is_online=True)
        clock.advance(TTL_SECONDS + 1)

        result = await sweeper.sweep_once()

        assert result.emitted == 1
        assert [entry.event_type for entry in _emitted(outbox)] == [OFFLINE]
        assert _emitted(outbox)[0].payload == {"player_id": str(ALICE)}

    async def test_the_event_is_stamped_with_the_lapse_not_the_sweep(
        self,
        presence: RedisPresenceProvider,
        sweeper: PresenceSweeper,
        outbox: InMemoryOutbox,
        clock: MovableClock,
    ) -> None:
        """A sweeper on a fifteen-second tick would otherwise report every
        departure as happening at a tick boundary — a fabrication, when the
        roster is scoring the exact instant."""
        await presence.record_presence(ALICE, is_online=True)
        deadline = clock.now() + timedelta(seconds=TTL_SECONDS)
        clock.advance(TTL_SECONDS + 47)

        await sweeper.sweep_once()

        assert _emitted(outbox)[0].occurred_at == deadline

    async def test_a_player_still_inside_their_window_is_left_alone(
        self,
        presence: RedisPresenceProvider,
        sweeper: PresenceSweeper,
        outbox: InMemoryOutbox,
        clock: MovableClock,
    ) -> None:
        await presence.record_presence(ALICE, is_online=True)
        clock.advance(TTL_SECONDS - 1)

        result = await sweeper.sweep_once()

        assert result.is_idle
        assert _emitted(outbox) == []

    async def test_an_empty_roster_is_an_idle_tick(self, sweeper: PresenceSweeper) -> None:
        """The common case: the sweeper runs on a timer and usually has
        nothing to do."""
        assert (await sweeper.sweep_once()).is_idle

    async def test_a_swept_player_is_not_swept_again(
        self,
        presence: RedisPresenceProvider,
        sweeper: PresenceSweeper,
        outbox: InMemoryOutbox,
        clock: MovableClock,
    ) -> None:
        """`forget` runs after the commit, so the second tick finds nothing —
        which is what stops one departure becoming an event per tick
        forever."""
        await presence.record_presence(ALICE, is_online=True)
        clock.advance(TTL_SECONDS + 1)

        await sweeper.sweep_once()
        await sweeper.sweep_once()

        assert len(_emitted(outbox)) == 1

    async def test_many_lapsed_players_are_one_batch(
        self,
        presence: RedisPresenceProvider,
        sweeper: PresenceSweeper,
        outbox: InMemoryOutbox,
        clock: MovableClock,
    ) -> None:
        """The case that motivates the batch: a node restarting takes every
        player it held offline at once."""
        for player in (ALICE, BOB, CAROL):
            await presence.record_presence(player, is_online=True)
        clock.advance(TTL_SECONDS + 1)

        result = await sweeper.sweep_once()

        assert result.emitted == 3
        assert {entry.payload["player_id"] for entry in _emitted(outbox)} == {
            str(ALICE),
            str(BOB),
            str(CAROL),
        }

    async def test_the_batch_size_bounds_one_tick(
        self,
        presence: RedisPresenceProvider,
        outbox: InMemoryOutbox,
        clock: MovableClock,
    ) -> None:
        """Bounded, per CLAUDE.md §10.5 — one transaction must not hold
        thousands of inserts because a tier restarted."""
        bounded = PresenceSweeper(
            roster=presence,
            presence=presence,
            events=OutboxEventPublisher(cast(Any, outbox)),
            unit_of_work=cast(Any, NullUnitOfWork()),
            clock=clock,
            batch_size=2,
        )
        for player in (ALICE, BOB, CAROL):
            await presence.record_presence(player, is_online=True)
        clock.advance(TTL_SECONDS + 1)

        assert (await bounded.sweep_once()).emitted == 2
        assert (await bounded.sweep_once()).emitted == 1

    async def test_the_sweeper_writes_no_presence_record(
        self,
        presence: RedisPresenceProvider,
        sweeper: PresenceSweeper,
        clock: MovableClock,
    ) -> None:
        """Writing `is_online=false` would create a *new* key with a fresh
        TTL and a `last_seen` of now — a record claiming the player was here
        at sweep time, which nobody observed."""
        await presence.record_presence(ALICE, is_online=True)
        clock.advance(TTL_SECONDS + 1)

        await sweeper.sweep_once()

        assert await presence.presence_for(ALICE) is None


class TestTheRaceItAvoids:
    """A player whose deadline passed and who signed in again *between* the
    sweeper's roster read and its emit.

    Driven through a stub roster rather than through the real adapter, and
    the reason is worth stating: the real adapter updates the record and the
    roster together, so it cannot *itself* produce "roster says lapsed while
    the record is live". That state exists only in the window between the
    sweeper's two reads — and reproducing it faithfully would mean
    interleaving two coroutines at a point neither controls.

    So the roster is stubbed to return what the sweeper would have read a
    moment ago, while the presence store — the real one — holds the live
    record the player just wrote. That is precisely the state the re-check
    exists for, with no concurrency in the test.
    """

    @staticmethod
    def _sweeper(
        presence: RedisPresenceProvider,
        outbox: InMemoryOutbox,
        clock: MovableClock,
        roster: "_StubRoster",
    ) -> PresenceSweeper:
        return PresenceSweeper(
            roster=cast(Any, roster),
            presence=presence,
            events=OutboxEventPublisher(cast(Any, outbox)),
            unit_of_work=cast(Any, NullUnitOfWork()),
            clock=clock,
            batch_size=100,
        )

    async def test_a_player_who_came_back_is_not_announced_as_gone(
        self, presence: RedisPresenceProvider, outbox: InMemoryOutbox, clock: MovableClock
    ) -> None:
        """Announcing then would say a present player had left, which is
        worse than the gap this class closes."""
        await presence.record_presence(ALICE, is_online=True)
        roster = _StubRoster([LapsedPresence(player_id=ALICE, lapsed_at=NOW)])

        result = await self._sweeper(presence, outbox, clock, roster).sweep_once()

        assert result.lapsed == 1
        assert result.emitted == 0
        assert _emitted(outbox) == []

    async def test_the_returning_player_is_still_cleared_from_the_roster(
        self, presence: RedisPresenceProvider, outbox: InMemoryOutbox, clock: MovableClock
    ) -> None:
        """Their sign-in already rewrote the score, so leaving the entry this
        tick read would mean re-reading them on every tick until they leave
        again."""
        await presence.record_presence(ALICE, is_online=True)
        roster = _StubRoster([LapsedPresence(player_id=ALICE, lapsed_at=NOW)])

        await self._sweeper(presence, outbox, clock, roster).sweep_once()

        assert roster.forgotten == [ALICE]

    async def test_one_player_returning_does_not_suppress_another_leaving(
        self, presence: RedisPresenceProvider, outbox: InMemoryOutbox, clock: MovableClock
    ) -> None:
        """The re-check is per player, not per batch."""
        await presence.record_presence(ALICE, is_online=True)
        roster = _StubRoster(
            [
                LapsedPresence(player_id=ALICE, lapsed_at=NOW),
                LapsedPresence(player_id=BOB, lapsed_at=NOW),
            ]
        )

        await self._sweeper(presence, outbox, clock, roster).sweep_once()

        assert [entry.payload["player_id"] for entry in _emitted(outbox)] == [str(BOB)]

    async def test_the_live_record_is_checked_in_one_read_for_the_batch(
        self, presence: RedisPresenceProvider, outbox: InMemoryOutbox, clock: MovableClock
    ) -> None:
        """`presence_for_many`, not a read per candidate — the sweeper's
        batch exists to keep a mass departure cheap, and a per-player
        re-check would put the N+1 straight back."""
        roster = _StubRoster(
            [LapsedPresence(player_id=player, lapsed_at=NOW) for player in (ALICE, BOB, CAROL)]
        )
        counting = _CountingPresence(presence)

        sweeper = PresenceSweeper(
            roster=cast(Any, roster),
            presence=cast(Any, counting),
            events=OutboxEventPublisher(cast(Any, outbox)),
            unit_of_work=cast(Any, NullUnitOfWork()),
            clock=clock,
            batch_size=100,
        )
        await sweeper.sweep_once()

        assert counting.batch_reads == 1


class TestFailureBehaviour:
    async def test_an_unreachable_redis_is_an_idle_tick(
        self, outbox: InMemoryOutbox, clock: MovableClock
    ) -> None:
        """A background sweep must never escalate: the roster entries are
        still there for the next tick."""
        broken = RedisPresenceProvider(
            cast(Redis, UnreachablePresenceRedis()),
            settings=PresenceSettings(ttl_seconds=TTL_SECONDS),
            clock=clock,
        )
        sweeper = PresenceSweeper(
            roster=broken,
            presence=broken,
            events=OutboxEventPublisher(cast(Any, outbox)),
            unit_of_work=cast(Any, NullUnitOfWork()),
            clock=clock,
            batch_size=100,
        )

        assert (await sweeper.sweep_once()).is_idle
        assert _emitted(outbox) == []

    async def test_a_failing_outbox_leaves_the_roster_intact(
        self,
        presence: RedisPresenceProvider,
        redis: FakePresenceRedis,
        clock: MovableClock,
    ) -> None:
        """Nothing is forgotten when nothing was recorded, so the next tick
        tries again — the alternative loses the transition permanently."""

        class _BrokenPublisher:
            async def publish(self, event: object) -> object:
                raise RuntimeError("outbox is unavailable")

        sweeper = PresenceSweeper(
            roster=presence,
            presence=presence,
            events=cast(Any, _BrokenPublisher()),
            unit_of_work=cast(Any, NullUnitOfWork()),
            clock=clock,
            batch_size=100,
        )
        await presence.record_presence(ALICE, is_online=True)
        clock.advance(TTL_SECONDS + 1)

        result = await sweeper.sweep_once()

        assert result.emitted == 0
        assert str(ALICE) in redis.roster(roster_key())


class _StubRoster:
    """A roster that answers with a fixed candidate list and records what it
    was asked to forget. See `TestTheRaceItAvoids` on why it is a stub."""

    def __init__(self, lapsed: list[LapsedPresence]) -> None:
        self._lapsed = lapsed
        self.forgotten: list[UUID] = []

    async def lapsed(self, *, now: datetime, limit: int) -> Sequence[LapsedPresence]:
        return self._lapsed[:limit]

    async def forget(self, player_ids: Sequence[UUID]) -> None:
        self.forgotten.extend(player_ids)


class _CountingPresence:
    """The real provider, with its batch read counted."""

    def __init__(self, inner: RedisPresenceProvider) -> None:
        self._inner = inner
        self.batch_reads = 0

    async def presence_for(self, player_id: UUID) -> Any:
        return await self._inner.presence_for(player_id)

    async def presence_for_many(self, player_ids: Sequence[UUID]) -> Mapping[UUID, Any]:
        self.batch_reads += 1
        return await self._inner.presence_for_many(player_ids)
