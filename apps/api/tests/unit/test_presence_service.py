"""`PresenceService` — the producer A64-013.6 added.

A64-012.7 built the presence store and left it with no writer; this file
asserts that the writer exists and writes what it claims. The three cases
the brief names — online recorded, offline recorded, `last_seen` updated —
are the three classes below, in that order.

Runs the **real** `RedisPresenceProvider` over `FakePresenceRedis`, for the
reason `test_presence.py` gives: substituting the store would leave this
suite asserting that a mock was called, which is true of any implementation
including a broken one. Here the service writes, the adapter encodes, the
fake stores, the adapter decodes and the service reads back — so a
regression anywhere along that path fails a test.

`MovableClock` is what makes the `last_seen` case an assertion rather than a
sleep (AD-07).
"""

from typing import cast
from uuid import UUID

import pytest
from redis.asyncio import Redis

from app.config.settings import PresenceSettings
from app.modules.users.application.services.presence_service import PresenceService
from app.modules.users.domain.presence import DeviceType
from app.modules.users.infrastructure.presence import (
    NoPresenceProvider,
    RedisPresenceProvider,
)
from tests.fakes.presence_redis import NOW, FakePresenceRedis, MovableClock

TTL_SECONDS = 60
PLAYER = UUID("019fbb1e-2b6a-7a41-9f0a-4b0f2a5c1d33")
SESSION = UUID("019fbb1e-3c11-7c2f-8a55-6d0b1e7c9a04")


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def redis(clock: MovableClock) -> FakePresenceRedis:
    return FakePresenceRedis(clock)


@pytest.fixture
def service(redis: FakePresenceRedis, clock: MovableClock) -> PresenceService:
    """The service over the real adapter, which is both its ports.

    `RedisPresenceProvider` satisfies `PresenceRecorder` and
    `PresenceProvider` both, and the composition root hands the same
    instance to each parameter — see `users.presentation.dependencies`. This
    fixture mirrors that rather than inventing a split the platform does not
    have.
    """
    provider = RedisPresenceProvider(
        cast(Redis, redis),
        settings=PresenceSettings(ttl_seconds=TTL_SECONDS),
        clock=clock,
    )
    return PresenceService(recorder=provider, provider=provider)


class TestGoingOnline:
    async def test_a_signed_in_player_is_recorded_as_present(
        self, service: PresenceService
    ) -> None:
        """The whole point of the task: a sign-in now produces presence."""
        await service.mark_online(PLAYER)

        recorded = await service.presence_of(PLAYER)

        assert recorded is not None
        assert recorded.is_online is True

    async def test_a_player_who_never_signed_in_has_no_presence(
        self, service: PresenceService
    ) -> None:
        """Absence reads as `None`, not as an offline record.

        The distinction matters to `PublicProfileComposer`: "never seen" and
        "seen and gone" render differently, and only the second has a
        `last_seen` worth showing.
        """
        assert await service.presence_of(PLAYER) is None

    async def test_the_session_is_recorded_and_the_device_with_it(
        self, service: PresenceService
    ) -> None:
        """Both optional observations survive the round trip.

        `session_id` is stored because a future challenge is routed to a
        connection rather than an account — and it is asserted here rather
        than through an endpoint precisely because no response schema
        carries it (A64-012.7: never expose internal session identifiers).
        """
        await service.mark_online(PLAYER, session_id=SESSION, device_type=DeviceType.MOBILE)

        recorded = await service.presence_of(PLAYER)

        assert recorded is not None
        assert recorded.session_id == str(SESSION)
        assert recorded.device_type is DeviceType.MOBILE

    async def test_recording_presence_twice_is_recording_it_once(
        self, service: PresenceService
    ) -> None:
        """Idempotent by construction — every write is the whole record.

        `POST /auth/refresh` calls this on a timer, so a second call must
        not accumulate anything; it restarts the window and nothing else.
        """
        await service.mark_online(PLAYER, session_id=SESSION)
        await service.mark_online(PLAYER, session_id=SESSION)

        recorded = await service.presence_of(PLAYER)

        assert recorded is not None
        assert recorded.is_online is True

    async def test_presence_expires_without_a_refresh(
        self, service: PresenceService, clock: MovableClock
    ) -> None:
        """The liveness protocol, asserted.

        This is what makes authentication a legitimate presence signal
        without a socket: nothing observes that a player closed the tab, so
        the record has to stop being true on its own. A player who stops
        refreshing stops being online one TTL later.
        """
        await service.mark_online(PLAYER)

        clock.advance(TTL_SECONDS + 1)

        assert await service.presence_of(PLAYER) is None


class TestGoingOffline:
    async def test_signing_out_everywhere_records_absence(self, service: PresenceService) -> None:
        """`POST /auth/logout-all` marks the player gone."""
        await service.mark_online(PLAYER)

        await service.mark_offline(PLAYER)

        recorded = await service.presence_of(PLAYER)
        assert recorded is not None
        assert recorded.is_online is False

    async def test_going_offline_writes_a_record_rather_than_deleting_one(
        self, service: PresenceService
    ) -> None:
        """The reason `mark_offline` is not a `DEL`.

        "Last seen four minutes ago" is only expressible if going offline
        leaves the timestamp behind. A delete would throw away the one
        field the record exists to carry.
        """
        await service.mark_online(PLAYER)
        clock_free_moment = await service.presence_of(PLAYER)
        assert clock_free_moment is not None

        await service.mark_offline(PLAYER)

        recorded = await service.presence_of(PLAYER)
        assert recorded is not None
        assert recorded.last_seen is not None

    async def test_a_player_who_was_never_here_can_still_be_marked_gone(
        self, service: PresenceService
    ) -> None:
        """No read-before-write, so revoking sessions never fails on a
        player whose presence had already expired."""
        await service.mark_offline(PLAYER)

        recorded = await service.presence_of(PLAYER)
        assert recorded is not None
        assert recorded.is_online is False


class TestLastSeen:
    async def test_refreshing_moves_last_seen_forward(
        self, service: PresenceService, clock: MovableClock
    ) -> None:
        """Every observation carries its own instant.

        This is what `POST /auth/refresh` buys: the record is rewritten
        whole on each exchange, so `last_seen` tracks the most recent proof
        the player was there.
        """
        await service.mark_online(PLAYER)
        first = await service.presence_of(PLAYER)
        assert first is not None

        clock.advance(30)
        await service.mark_online(PLAYER)

        second = await service.presence_of(PLAYER)
        assert second is not None
        assert second.last_seen is not None and first.last_seen is not None
        assert second.last_seen > first.last_seen

    async def test_going_offline_stamps_the_moment_it_happened(
        self, service: PresenceService, clock: MovableClock
    ) -> None:
        """`last_seen` after a sign-out is the sign-out, not the sign-in."""
        await service.mark_online(PLAYER)
        clock.advance(120)

        await service.mark_offline(PLAYER)

        recorded = await service.presence_of(PLAYER)
        assert recorded is not None
        assert recorded.last_seen == clock.now()

    async def test_the_instant_comes_from_the_clock_and_not_from_the_wall(
        self, service: PresenceService, clock: MovableClock
    ) -> None:
        """AD-07, asserted rather than assumed.

        A `datetime.now()` anywhere on this path would make the value
        unequal to the injected clock — and would make every assertion above
        depend on how long the test took to run.
        """
        await service.mark_online(PLAYER)

        recorded = await service.presence_of(PLAYER)
        assert recorded is not None
        assert recorded.last_seen == NOW


class TestTheKillSwitch:
    async def test_presence_disabled_records_nothing_and_raises_nothing(self) -> None:
        """`PRESENCE_ENABLED=false` must not break a sign-in.

        The service holds `NoPresenceProvider` on both ports, accepts every
        write and reports no presence — a cosmetic feature switched off,
        not an endpoint switched off.
        """
        disabled = NoPresenceProvider()
        service = PresenceService(recorder=disabled, provider=disabled)

        await service.mark_online(PLAYER, session_id=SESSION)
        await service.mark_offline(PLAYER)

        assert await service.presence_of(PLAYER) is None
