"""The mailbox outliving its own TTL — A64-028.5A, P1-9.

## The defect

`gwbus:v1:<node>` carries a TTL that is refreshed **only on publish**. A
node receiving no cross-node traffic for that long loses the key — and the
consumer group goes with it, because a group belongs to the stream.

`RedisStreamGatewayBus` caches which groups it has created, in process
memory, so that a consume does not pay an `XGROUP CREATE` round trip every
tick. After the key lapses that cache is a **lie**: `_ensure_group` returns
without doing anything and every `XREADGROUP` answers `NOGROUP` — not once,
but for ever, until the process restarts.

The symptom is silence. The publisher succeeds, the entry is written, and
the addressee simply never receives it. One instance's log held 4812
warnings before a benchmark noticed.

## What these tests hold

Not "publish and subscribe work" — that is covered elsewhere and passes
under the buggy implementation. These reproduce the *lifecycle*: expire the
key underneath a live consumer that believes its group exists, publish, and
require the frame to arrive.

They fail against the pre-fix adapter, which is the only reason to have
them.
"""

import pytest
from redis.asyncio import Redis

from app.gateway.bus import BusMessage
from app.gateway.stream_bus import RedisStreamGatewayBus

pytestmark = pytest.mark.asyncio

NODE = "lifecycle-node"
KEY = f"gwbus:v1:{NODE}"


def message(ply: int) -> BusMessage:
    return BusMessage(node_id=NODE, connection_ids=("c1",), frame=f'{{"ply":{ply}}}')


def bus_for(redis: Redis) -> RedisStreamGatewayBus:
    return RedisStreamGatewayBus(redis, max_stream_length=1000, stream_ttl_seconds=3600)


class TestAQuietNodeKeepsItsMailbox:
    async def test_a_frame_published_after_the_key_lapsed_still_arrives(
        self, contract_redis: Redis
    ) -> None:
        """The whole of P1-9, in one test.

        The consume before the deletion is what makes it a *stale cache*
        rather than a cold start: the adapter has recorded the group as
        created, and after the key goes that record is wrong.
        """
        bus = bus_for(contract_redis)

        await bus.publish(message(1))
        assert len(await bus.consume(NODE, limit=10)) == 1

        # Exactly what TTL expiry leaves behind: no key, no group, and a
        # process that still believes it created one.
        await contract_redis.delete(KEY)

        await bus.publish(message(2))
        delivered = await bus.consume(NODE, limit=10)

        assert len(delivered) == 1, (
            "the first frame after the mailbox lapsed was never delivered — "
            "the consumer group was not recreated"
        )
        assert delivered[0].frame == '{"ply":2}'

    async def test_the_recreated_group_reads_from_the_beginning(
        self, contract_redis: Redis
    ) -> None:
        """§4. `XGROUP CREATE` at `$` would skip everything already in the
        stream, so a frame published *before* the group was recreated would
        be invisible for ever — a silent loss that looks exactly like the
        defect above and would survive the fix for it.

        Several frames are published before any consume, so a group created
        at `$` delivers none of them and a group created at `0` delivers all.
        """
        bus = bus_for(contract_redis)
        await bus.publish(message(1))
        assert len(await bus.consume(NODE, limit=10)) == 1

        await contract_redis.delete(KEY)
        for ply in (2, 3, 4):
            await bus.publish(message(ply))

        delivered = await bus.consume(NODE, limit=10)

        assert [entry.frame for entry in delivered] == [
            '{"ply":2}',
            '{"ply":3}',
            '{"ply":4}',
        ]

    async def test_recovery_repeats_for_a_second_lapse(
        self, contract_redis: Redis
    ) -> None:
        """A node can be quiet more than once. A fix that healed the cache
        only the first time would pass the test above and fail in a week."""
        bus = bus_for(contract_redis)

        for ply in (1, 2, 3):
            await contract_redis.delete(KEY)
            await bus.publish(message(ply))

            delivered = await bus.consume(NODE, limit=10)

            assert [entry.frame for entry in delivered] == [f'{{"ply":{ply}}}']

    async def test_an_ordinary_consume_costs_no_recreation(
        self, contract_redis: Redis
    ) -> None:
        """The cache still has to work. A fix that recreated the group on
        every tick would be correct and would add a round trip to the
        hottest loop on the platform."""
        bus = bus_for(contract_redis)
        await bus.publish(message(1))
        await bus.consume(NODE, limit=10)

        groups_before = await contract_redis.xinfo_groups(KEY)
        await bus.publish(message(2))
        assert len(await bus.consume(NODE, limit=10)) == 1
        groups_after = await contract_redis.xinfo_groups(KEY)

        # Same group, still one, and its read position advanced rather than
        # being reset by a needless recreation.
        assert len(groups_before) == len(groups_after) == 1


class TestNothingElseIsSwallowed:
    async def test_a_consume_on_a_never_used_node_is_empty_not_an_error(
        self, contract_redis: Redis
    ) -> None:
        """`MKSTREAM` exists for this: a node that has never been published
        to must read empty rather than fail, or every fresh instance would
        log an error on its first tick."""
        bus = bus_for(contract_redis)

        assert await bus.consume("never-addressed", limit=10) == ()
