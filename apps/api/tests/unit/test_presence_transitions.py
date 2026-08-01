"""`PresenceNotificationService` — transition detection, A64-013.7.

"Presence notifications are emitted only on state transitions ... do NOT
emit events on repeated refreshes. Detect edge transitions."

That is one rule with two directions and one very common non-case, and this
file is the three of them:

    TestGoingOnline    offline -> online emits; online -> online does not
    TestGoingOffline   online -> offline emits; offline -> offline does not
    TestRefreshes      the volume case, stated on its own because it is the
                       reason the rule exists

Runs the **real** service over the real `PresenceService` over the real
`RedisPresenceProvider` over `FakePresenceRedis`, and a real
`OutboxEventPublisher` over an in-memory outbox. What is faked is Redis and
the table; what is under test — the read-compare-write sequence — is not
substituted, because a fake presence port would let this suite pass while
the edge was computed from the record it had just written.
"""

from typing import Any, cast
from uuid import UUID

import pytest
from redis.asyncio import Redis

from app.config.settings import PresenceSettings
from app.modules.notifications.application.services import PresenceNotificationService
from app.modules.users.application.services.presence_service import PresenceService
from app.modules.users.infrastructure.presence import RedisPresenceProvider
from app.platform.outbox import NoEventPublisher, OutboxEntry, OutboxEventPublisher
from tests.fakes.outbox import InMemoryOutbox, NullUnitOfWork
from tests.fakes.presence_redis import NOW, FakePresenceRedis, MovableClock

TTL_SECONDS = 60
PLAYER = UUID("019fbc20-2222-7000-8000-000000000002")
SESSION = UUID("019fbc20-3333-7000-8000-000000000003")

ONLINE = "users.presence_online"
OFFLINE = "users.presence_offline"


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def outbox() -> InMemoryOutbox:
    return InMemoryOutbox()


@pytest.fixture
def service(clock: MovableClock, outbox: InMemoryOutbox) -> PresenceNotificationService:
    presence = RedisPresenceProvider(
        cast(Redis, FakePresenceRedis(clock)),
        settings=PresenceSettings(ttl_seconds=TTL_SECONDS),
        clock=clock,
    )
    return PresenceNotificationService(
        presence=PresenceService(recorder=presence, provider=presence),
        events=OutboxEventPublisher(cast(Any, outbox)),
        unit_of_work=cast(Any, NullUnitOfWork()),
        clock=clock,
    )


def _emitted(outbox: InMemoryOutbox) -> list[str]:
    """The event types recorded, in the order they were published.

    Insertion order, not `(occurred_at, id)`. Several of the cases below
    emit twice from one fixed clock instant, and a v7 id is only *nearly*
    monotonic inside a millisecond — sorting on it made this helper return
    whichever order the generator happened to produce, which is a flaky test
    dressed up as an ordering assertion. `dict` preserves insertion order,
    and insertion order is exactly what "the service emitted these, in this
    sequence" means.
    """
    return [entry.event_type for entry in outbox.entries.values()]


class TestGoingOnline:
    async def test_a_player_who_was_offline_produces_an_event(
        self, service: PresenceNotificationService, outbox: InMemoryOutbox
    ) -> None:
        """The `offline -> online` edge. A sign-in by somebody who was not
        here is the fact a friend wants to be told about."""
        await service.record_online(PLAYER, session_id=SESSION)

        assert _emitted(outbox) == [ONLINE]

    async def test_a_player_who_was_already_online_produces_nothing(
        self, service: PresenceNotificationService, outbox: InMemoryOutbox
    ) -> None:
        """No edge, no event — the rule the brief states outright."""
        await service.record_online(PLAYER)
        await service.record_online(PLAYER)

        assert _emitted(outbox) == [ONLINE]

    async def test_presence_is_still_recorded_when_no_event_is_emitted(
        self, service: PresenceNotificationService, clock: MovableClock
    ) -> None:
        """Suppressing the *event* must not suppress the *write*: the second
        call is what restarts the TTL, and a player whose refresh stopped
        writing would go dark while still signed in."""
        await service.record_online(PLAYER)
        clock.advance(50)
        await service.record_online(PLAYER)
        clock.advance(50)

        # 100 seconds after the first write, with a 60-second window: only
        # the second write can be keeping this alive.
        assert await service._presence.presence_of(PLAYER) is not None  # noqa: SLF001

    async def test_coming_back_after_a_lapse_is_a_new_edge(
        self, service: PresenceNotificationService, outbox: InMemoryOutbox, clock: MovableClock
    ) -> None:
        """A player who closed the tab, lapsed, and signed in again is
        offline-to-online a second time — because the record expired, and an
        expired record reads the same as no record at all."""
        await service.record_online(PLAYER)
        clock.advance(TTL_SECONDS + 1)
        await service.record_online(PLAYER)

        assert _emitted(outbox) == [ONLINE, ONLINE]

    async def test_the_event_names_the_player_and_nothing_else(
        self, service: PresenceNotificationService, outbox: InMemoryOutbox
    ) -> None:
        """No `session_id`, though one was passed and is recorded in Redis.
        It reaches no response schema (A64-012.7), so it does not go in the
        durable log either — an outbox payload is one `SELECT` from an
        operator's terminal."""
        await service.record_online(PLAYER, session_id=SESSION)

        entry = _only(outbox)
        assert entry.payload == {"player_id": str(PLAYER)}


class TestGoingOffline:
    async def test_an_online_player_signing_out_everywhere_produces_an_event(
        self, service: PresenceNotificationService, outbox: InMemoryOutbox
    ) -> None:
        await service.record_online(PLAYER)
        await service.record_offline(PLAYER)

        assert _emitted(outbox) == [ONLINE, OFFLINE]

    async def test_a_player_who_was_never_here_produces_nothing(
        self, service: PresenceNotificationService, outbox: InMemoryOutbox
    ) -> None:
        """`logout-all` by somebody whose presence had already lapsed is not
        a transition — there is nobody to tell that a friend who was not
        visible has stopped being visible."""
        await service.record_offline(PLAYER)

        assert _emitted(outbox) == []

    async def test_signing_out_twice_produces_one_event(
        self, service: PresenceNotificationService, outbox: InMemoryOutbox
    ) -> None:
        await service.record_online(PLAYER)
        await service.record_offline(PLAYER)
        await service.record_offline(PLAYER)

        assert _emitted(outbox) == [ONLINE, OFFLINE]

    async def test_offline_is_still_recorded_when_no_event_is_emitted(
        self, service: PresenceNotificationService
    ) -> None:
        """The record is written whichever way the edge check goes — that is
        what makes "last seen" available for a player who was already absent
        when they revoked their sessions."""
        await service.record_offline(PLAYER)

        recorded = await service._presence.presence_of(PLAYER)  # noqa: SLF001
        assert recorded is not None and recorded.is_online is False


class TestRefreshes:
    async def test_a_signed_in_player_refreshing_all_day_emits_once(
        self, service: PresenceNotificationService, outbox: InMemoryOutbox, clock: MovableClock
    ) -> None:
        """The case the whole rule exists for.

        Twenty refreshes inside the presence window would be twenty outbox
        rows, twenty relay ticks, twenty audience resolutions and twenty
        fan-outs to every friend — for a state that never changed.
        """
        await service.record_online(PLAYER)
        for _ in range(20):
            clock.advance(30)
            await service.record_online(PLAYER)

        assert _emitted(outbox) == [ONLINE]

    async def test_the_full_lifecycle_emits_exactly_two_events(
        self, service: PresenceNotificationService, outbox: InMemoryOutbox, clock: MovableClock
    ) -> None:
        """Sign in, refresh several times, sign out of everything: two
        edges, whatever happened in between."""
        await service.record_online(PLAYER, session_id=SESSION)
        for _ in range(3):
            clock.advance(20)
            await service.record_online(PLAYER, session_id=SESSION)
        await service.record_offline(PLAYER)

        assert _emitted(outbox) == [ONLINE, OFFLINE]


class TestFailureBehaviour:
    async def test_a_sign_in_survives_an_outbox_that_cannot_record(
        self, clock: MovableClock
    ) -> None:
        """`OUTBOX_ENABLED=false`, or a failing publish: the presence write
        still happens and nothing raises. A sign-in must not fail because a
        notification could not be queued."""
        presence = RedisPresenceProvider(
            cast(Redis, FakePresenceRedis(clock)),
            settings=PresenceSettings(ttl_seconds=TTL_SECONDS),
            clock=clock,
        )
        service = PresenceNotificationService(
            presence=PresenceService(recorder=presence, provider=presence),
            events=NoEventPublisher(),
            unit_of_work=cast(Any, NullUnitOfWork()),
            clock=clock,
        )

        await service.record_online(PLAYER)

        recorded = await service._presence.presence_of(PLAYER)  # noqa: SLF001
        assert recorded is not None and recorded.is_online is True

    async def test_the_event_carries_the_injected_clock_s_instant(
        self, service: PresenceNotificationService, outbox: InMemoryOutbox, clock: MovableClock
    ) -> None:
        """AD-07. `occurred_at` is the outbox's ordering key, so a
        wall-clock read here would order events by when the process noticed
        rather than by when they happened."""
        clock.advance(3600)
        await service.record_online(PLAYER)

        assert _only(outbox).occurred_at == clock.now()
        assert _only(outbox).occurred_at != NOW


def _only(outbox: InMemoryOutbox) -> OutboxEntry:
    assert len(outbox.entries) == 1, f"expected one event, found {len(outbox.entries)}"
    return next(iter(outbox.entries.values()))
