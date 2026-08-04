"""The spectator store against real Redis — A64-016.7 §9.

`tests/unit/test_gateway_connection.py` covers what the *handler* and the
*fan-out* decide — eligibility, the safe snapshot, which tab receives a move,
which events an audience may see — over a fake that models subscriptions as a
set. This covers what only Redis has: that a lapsed subscription genuinely
stops being an audience member without anything running to remove it, and
that the reverse index a disconnect reads is written by the same transaction
that created the subscription.

One test, because §9 caps the phase at eight and seven handler behaviours
plus one for the store is the split that puts each assertion where it can
only be proven.

Skipped, not failed, when Redis is unreachable.
"""

from datetime import UTC, datetime

import pytest_asyncio
from redis.asyncio import Redis

from app.core.identifiers import generate_uuid7
from app.gateway.spectator_store import RedisSpectatorStore
from app.gateway.spectators import SpectatorSubscription
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest_asyncio.fixture
async def store(contract_redis: Redis, clock: MovableClock) -> RedisSpectatorStore:
    return RedisSpectatorStore(contract_redis, clock=clock)


class TestTheSpectatorStore:
    async def test_subscriptions_lapse_by_score_and_a_disconnect_clears_every_match(
        self, store: RedisSpectatorStore, clock: MovableClock
    ) -> None:
        """The two properties the audience's correctness rests on.

        **A lapsed subscription is not an audience member.** The score is
        the expiry and `routes_for` reads a score range, so a viewer whose
        subscription ran out stops being fanned out to at the instant it
        did — with nothing scheduled, nothing sweeping and no key deleted.
        That matters because the alternative is a set that only shrinks when
        somebody remembers to prune it, and the thing that would have
        remembered is the node that died.

        The **key** TTL is deliberately longer than the member's, which is
        the same margin `gwroom:v1:` keeps: Redis drops a sorted set when
        its last member is *removed*, never when one merely expired by
        score, so a key whose members have all lapsed must still go on its
        own.

        **A disconnect clears every match.** A socket that dropped never
        sends `spectator.leave`, so the reverse index is what lets cleanup
        remove a connection from audiences nobody recorded a list of — and
        it is written by the same transaction as the subscription, so there
        is no window in which one exists without the other.

        Two tabs of one viewer are two subscriptions throughout, which is
        why `SpectatorSubscription` carries the connection: closing one must
        not remove the other from the fan-out.
        """
        viewer, other_viewer = generate_uuid7(), generate_uuid7()
        first_match, second_match = generate_uuid7(), generate_uuid7()
        watching = SpectatorSubscription(player_id=viewer, connection_id=generate_uuid7())
        second_tab = SpectatorSubscription(player_id=viewer, connection_id=generate_uuid7())
        stranger = SpectatorSubscription(player_id=other_viewer, connection_id=generate_uuid7())

        assert await store.subscribe(first_match, watching, ttl_seconds=300) == 1
        assert await store.subscribe(first_match, second_tab, ttl_seconds=300) == 2
        # Idempotent on `(player, connection)`: the same tab pressing watch
        # twice is one audience member, not two.
        assert await store.subscribe(first_match, watching, ttl_seconds=300) == 2
        await store.subscribe(second_match, watching, ttl_seconds=300)

        # A viewer whose subscription is shorter than everybody else's.
        await store.subscribe(first_match, stranger, ttl_seconds=30)

        assert set(await store.routes_for(first_match)) == {watching, second_tab, stranger}

        # Past the stranger's expiry and inside everyone else's. Nothing ran.
        clock.advance(60)
        assert set(await store.routes_for(first_match)) == {watching, second_tab}

        # The disconnect path: one tab, every match it was watching.
        assert set(await store.unsubscribe_all(watching)) == {first_match, second_match}
        assert set(await store.routes_for(first_match)) == {second_tab}
        assert await store.routes_for(second_match) == ()

        # The other tab is untouched and can still leave on its own.
        assert await store.unsubscribe(first_match, second_tab) == 0
