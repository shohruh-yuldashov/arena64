"""The presence adapters — A64-012.7.

A64-012.7 asks for essential tests only and names five, all of which are
composition-level and live in `tests/unit/test_profile_service.py` beside
the privacy rules they exercise. This file covers the layer beneath them:
what the Redis adapter actually stores, and what it does when the store
misbehaves.

Both are here rather than in a contract suite because they are properties of
*this code* rather than of Redis. What the adapter writes, how it decodes,
which failures it swallows and which values it refuses are all decisions
made in `presence_providers.py`; a suite that needed Docker to assert them
would be skipped on most machines, and these are exactly the paths nobody
exercises by hand.

The one Redis behaviour the correctness of presence depends on — a key
expiring on its own — is modelled by `FakePresenceRedis`, and is asserted
here because "presence must automatically expire" is a requirement rather
than an implementation detail. See that fake on where the line is drawn.

Runs the **real** `RedisPresenceProvider` throughout. Substituting the thing
under test would leave the encoding, the key derivation and the "never
raises" promise untested, which is where the interesting failures are.
"""

import json
from typing import cast
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis

from app.config.settings import PresenceSettings
from app.modules.users.domain.presence import DeviceType
from app.modules.users.infrastructure.presence import (
    NoPresenceProvider,
    RedisPresenceProvider,
    presence_key,
)
from tests.fakes.presence_redis import (
    NOW,
    FakePresenceRedis,
    MovableClock,
    UnreachablePresenceRedis,
)

TTL_SECONDS = 60
PLAYER = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def settings() -> PresenceSettings:
    return PresenceSettings(ttl_seconds=TTL_SECONDS)


@pytest.fixture
def redis(clock: MovableClock) -> FakePresenceRedis:
    return FakePresenceRedis(clock)


@pytest.fixture
def provider(
    redis: FakePresenceRedis, settings: PresenceSettings, clock: MovableClock
) -> RedisPresenceProvider:
    # `cast` rather than a narrowed port on the adapter: this platform's
    # other Redis adapter (`RedisRateLimiter`) also takes the concrete
    # client, and introducing an abstraction over a driver purely to make a
    # test compile would be shape without a consumer (CLAUDE.md §1 rule 7).
    return RedisPresenceProvider(cast(Redis, redis), settings=settings, clock=clock)


class TestRecordingAnObservation:
    async def test_an_online_player_reads_back_as_online(
        self, provider: RedisPresenceProvider
    ) -> None:
        await provider.record_presence(PLAYER, is_online=True)

        presence = await provider.presence_for(PLAYER)

        assert presence is not None
        assert presence.is_online is True
        assert presence.last_seen == NOW

    async def test_a_disconnect_is_recorded_rather_than_erased(
        self, provider: RedisPresenceProvider, clock: MovableClock
    ) -> None:
        """`is_online=False` is what makes "last seen four minutes ago"
        possible. Deleting the key instead would throw away the timestamp
        the record exists to carry, leaving nothing to distinguish a player
        who just closed a tab from one who has never connected."""
        await provider.record_presence(PLAYER, is_online=True)
        clock.advance(seconds=10)
        await provider.record_presence(PLAYER, is_online=False)

        presence = await provider.presence_for(PLAYER)

        assert presence is not None
        assert presence.is_online is False
        assert presence.last_seen == NOW.replace(second=10)

    async def test_the_timestamp_comes_from_the_injected_clock(
        self, provider: RedisPresenceProvider, clock: MovableClock
    ) -> None:
        """AD-07. The value ends up on a profile, so a test asserting it must
        not have to sleep — and a wall-clock read here would make this
        assertion a race."""
        clock.advance(seconds=3600)

        await provider.record_presence(PLAYER, is_online=True)
        presence = await provider.presence_for(PLAYER)

        assert presence is not None
        assert presence.last_seen == clock.now()

    async def test_a_later_observation_replaces_the_earlier_one(
        self, provider: RedisPresenceProvider, clock: MovableClock
    ) -> None:
        """Last writer wins, whole record. Two gateway nodes observing the
        same player seconds apart must produce one of two complete records
        rather than a mixture — which is what makes this correct on fifty
        nodes without any coordination."""
        await provider.record_presence(
            PLAYER, is_online=True, session_id="first", device_type=DeviceType.WEB
        )
        clock.advance(seconds=5)
        await provider.record_presence(PLAYER, is_online=True, session_id="second")

        presence = await provider.presence_for(PLAYER)

        assert presence is not None
        assert presence.session_id == "second"
        # Not carried over from the earlier record: a merge would preserve a
        # field from an observation that is by definition older.
        assert presence.device_type is None


class TestExpiry:
    async def test_presence_expires_without_being_rewritten(
        self, provider: RedisPresenceProvider, clock: MovableClock
    ) -> None:
        """The requirement, and the liveness protocol. Nothing tells the
        platform that a gateway node died, so the only thing stopping its
        players being marked online forever is the record lapsing on its
        own."""
        await provider.record_presence(PLAYER, is_online=True)

        clock.advance(seconds=TTL_SECONDS + 1)

        assert await provider.presence_for(PLAYER) is None

    async def test_an_observation_inside_the_window_extends_it(
        self, provider: RedisPresenceProvider, clock: MovableClock
    ) -> None:
        """A present player stays present. If the TTL were set once and
        never reset, everyone would flicker offline a minute after
        connecting however active they were."""
        await provider.record_presence(PLAYER, is_online=True)

        clock.advance(seconds=TTL_SECONDS - 1)
        await provider.record_presence(PLAYER, is_online=True)
        clock.advance(seconds=TTL_SECONDS - 1)

        assert await provider.presence_for(PLAYER) is not None

    async def test_an_expired_record_is_indistinguishable_from_one_never_written(
        self, provider: RedisPresenceProvider, clock: MovableClock
    ) -> None:
        """A64-012.7: the response must not let a client tell "hidden" from
        "unavailable" from "not yet recorded". The adapter is where the last
        two become the same value; the profile mapping is where the first
        joins them."""
        await provider.record_presence(PLAYER, is_online=True)
        clock.advance(seconds=TTL_SECONDS + 1)

        expired = await provider.presence_for(PLAYER)
        never_written = await provider.presence_for(uuid4())

        assert expired is never_written is None


class TestNothingLeaksThroughTheRecord:
    async def test_the_key_carries_the_player_id_and_a_version(self) -> None:
        """Asserted so that changing the keyspace is a deliberate act. A
        gateway node and an API node are separately deployed halves of one
        contract, and a silently renamed key is presence that stops working
        for everyone mid-rollout."""
        assert presence_key(PLAYER) == f"presence:v1:{PLAYER}"

    async def test_session_and_device_round_trip_but_reach_no_response(
        self, provider: RedisPresenceProvider, redis: FakePresenceRedis
    ) -> None:
        """Both are recorded — the keyspace has to have room for them from
        the first release — and neither has a field on any response schema
        to land in. Asserted on the *type* rather than on a serialised body,
        because that is what makes "never expose internal session
        identifiers" structural."""
        from app.modules.profiles.presentation.schemas import (
            MyProfileResponse,
            ProfileResponse,
        )

        await provider.record_presence(
            PLAYER, is_online=True, session_id="gw-7f3a", device_type=DeviceType.MOBILE
        )
        presence = await provider.presence_for(PLAYER)

        assert presence is not None
        assert presence.session_id == "gw-7f3a"
        assert presence.device_type is DeviceType.MOBILE
        for schema in (ProfileResponse, MyProfileResponse):
            assert "session_id" not in schema.model_fields
            assert "device_type" not in schema.model_fields

    async def test_an_unset_field_is_omitted_rather_than_stored_as_null(
        self, provider: RedisPresenceProvider, redis: FakePresenceRedis
    ) -> None:
        """Keeps the stored value the size of what was actually observed —
        one key per online player is the platform's highest-cardinality
        Redis workload."""
        await provider.record_presence(PLAYER, is_online=True)

        stored = await redis.get(presence_key(PLAYER))

        assert stored is not None
        assert set(json.loads(stored)) == {"online", "last_seen"}


class TestDegradation:
    async def test_an_unreachable_store_reads_as_unknown_rather_than_raising(
        self, settings: PresenceSettings, clock: MovableClock
    ) -> None:
        """The promise on `PresenceProvider.presence_for`. Failing here
        would take down the platform's highest-volume public read for an
        indicator that is cosmetic by design (system-design.md §626)."""
        provider = RedisPresenceProvider(
            cast(Redis, UnreachablePresenceRedis()), settings=settings, clock=clock
        )

        assert await provider.presence_for(PLAYER) is None

    async def test_a_failed_write_does_not_raise_either(
        self, settings: PresenceSettings, clock: MovableClock
    ) -> None:
        """A gateway must not drop a socket because a presence write timed
        out. The next observation writes the whole record anyway, so one
        lost write self-heals within the refresh interval."""
        provider = RedisPresenceProvider(
            cast(Redis, UnreachablePresenceRedis()), settings=settings, clock=clock
        )

        # Returns nothing; what is asserted is that it does not raise —
        # an unreachable Redis must not fail the request that observed
        # the player.
        await provider.record_presence(PLAYER, is_online=True)

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param("not json at all", id="not-json"),
            pytest.param('["online"]', id="not-an-object"),
            pytest.param('{"last_seen": "2026-08-01T12:00:00+00:00"}', id="no-online-flag"),
            pytest.param(
                '{"online": "yes", "last_seen": "2026-08-01T12:00:00+00:00"}',
                id="online-not-a-boolean",
            ),
            pytest.param('{"online": true}', id="no-timestamp"),
            pytest.param(
                '{"online": true, "last_seen": "yesterday"}', id="timestamp-not-an-instant"
            ),
            pytest.param(
                '{"online": true, "last_seen": "2026-08-01T12:00:00"}',
                id="timestamp-without-an-offset",
            ),
        ],
    )
    async def test_a_record_that_cannot_be_trusted_is_discarded_whole(
        self, provider: RedisPresenceProvider, redis: FakePresenceRedis, stored: str
    ) -> None:
        """Strict about the two fields that carry meaning. Defaulting either
        would publish a fact nobody observed, on a field governed by a
        privacy flag — so the fail-safe direction is to report nothing.

        A naive offset is refused rather than assumed to be UTC: guessing
        would silently shift a published timestamp by hours (DM-14).
        """
        redis.poison(presence_key(PLAYER), stored)

        assert await provider.presence_for(PLAYER) is None

    async def test_an_unknown_device_type_does_not_discard_the_record(
        self, provider: RedisPresenceProvider, redis: FakePresenceRedis
    ) -> None:
        """Tolerant where strictness would cost something and buy nothing.
        A newer gateway node writing a fourth device type during a rolling
        deploy must not stop older API nodes from rendering presence — and
        the field is never published, so an unrecognised value costs
        nothing."""
        redis.poison(
            presence_key(PLAYER),
            json.dumps(
                {
                    "online": True,
                    "last_seen": "2026-08-01T12:00:00+00:00",
                    "device_type": "smart_fridge",
                }
            ),
        )

        presence = await provider.presence_for(PLAYER)

        assert presence is not None
        assert presence.is_online is True
        assert presence.device_type is None


class TestNoPresenceProvider:
    """The fallback, wired by `PRESENCE_ENABLED=false`."""

    async def test_nobody_is_ever_observed(self) -> None:
        provider = NoPresenceProvider()

        assert await provider.presence_for(PLAYER) is None
        assert await provider.presence_for(uuid4()) is None

    async def test_writes_are_accepted_and_discarded(self) -> None:
        """Silently, and deliberately: this is what a gateway holds when
        presence is switched off, and a recorder that raised would turn a
        kill switch for a cosmetic feature into a broken connect path."""
        provider = NoPresenceProvider()

        await provider.record_presence(PLAYER, is_online=True, session_id="gw-1")

        assert await provider.presence_for(PLAYER) is None

    async def test_its_answer_is_the_one_the_real_provider_gives_for_a_stranger(
        self, provider: RedisPresenceProvider
    ) -> None:
        """The honest-degradation claim, pinned. A deployment on the
        fallback serves a value clients already handle rather than a special
        case they do not — which is what separates this fallback from the
        statistics one, whose zeroes read as a beginner's record."""
        assert await NoPresenceProvider().presence_for(PLAYER) == await provider.presence_for(
            PLAYER
        )
