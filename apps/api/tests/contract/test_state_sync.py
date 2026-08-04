"""The reconnect buffer against real Redis — A64-016.6 §9.

`tests/unit/test_gateway_connection.py` covers what the *handler* decides —
events versus snapshot, the membership check, the payload projection — over a
fake that models the buffer's two rules. This covers what only Redis has:
that the trim, the idempotent append and the continuity proof actually behave
that way under the Lua script that implements them.

One test, because §9 caps the phase at eight and the six handler behaviours
plus one for the whole store is the split that puts each assertion where it
can only be proven.

Skipped, not failed, when Redis is unreachable.
"""

import pytest_asyncio
from redis.asyncio import Redis

from app.core.identifiers import generate_uuid7
from app.gateway.event_buffer import RedisMatchEventBuffer


@pytest_asyncio.fixture
async def buffer(contract_redis: Redis) -> RedisMatchEventBuffer:
    # Four entries, so the trim is reachable in a readable number of writes.
    return RedisMatchEventBuffer(contract_redis, max_events=4, ttl_seconds=3600)


class TestTheEventBuffer:
    async def test_it_is_idempotent_bounded_and_honest_about_continuity(
        self, buffer: RedisMatchEventBuffer, contract_redis: Redis
    ) -> None:
        """The three properties the reconnect decision rests on, against the
        script that provides them.

        **Idempotent on the sequence.** The fan-out is at-least-once, so a
        frame redelivered by the bus must not appear twice in a resuming
        client's replay. `ZADD` on an existing member is a no-op and the
        member carries the sequence, so this is structural rather than a
        check.

        **Bounded by rank.** A hundred-ply game must not accumulate a
        hundred frames forever. The oldest go, which is the right end: a
        client too far behind for the buffer gets a snapshot, and one that
        missed the newest ply is looking at a stale board.

        **Continuity is proven by the oldest entry, not the count.** This is
        the assertion that matters most and the one a naive implementation
        gets wrong: after trimming, the buffer still *has* frames for a
        client at sequence 1 — and returning them would leave that client
        silently missing plies 2 and 3 while believing it was current, which
        is the partial recovery §6 forbids.

        The TTL is asserted too: a capped buffer is still one key per match
        ever played, which is unbounded in history rather than in size.
        """
        match_id = generate_uuid7()

        for ply in (1, 2, 3):
            await buffer.append(match_id, sequence=ply, frame=f'{{"ply":{ply}}}')
        # The same event again — a redelivery, which must be a no-op.
        await buffer.append(match_id, sequence=2, frame='{"ply":2}')

        assert await buffer.length(match_id) == 3

        covered = await buffer.since(match_id, sequence=1)
        assert covered.is_contiguous
        assert covered.frames == ('{"ply":2}', '{"ply":3}')

        # A client that has seen everything: contiguous, and nothing to send.
        current = await buffer.since(match_id, sequence=3)
        assert current.is_contiguous
        assert current.frames == ()

        # Past the bound. Four kept, so plies 1 and 2 are gone.
        for ply in (4, 5, 6):
            await buffer.append(match_id, sequence=ply, frame=f'{{"ply":{ply}}}')

        assert await buffer.length(match_id) == 4

        stale = await buffer.since(match_id, sequence=1)
        assert not stale.is_contiguous
        # It *has* frames — which is exactly why the caller must branch on
        # continuity rather than on this being empty.
        assert stale.frames != ()

        still_covered = await buffer.since(match_id, sequence=4)
        assert still_covered.is_contiguous
        assert still_covered.frames == ('{"ply":5}', '{"ply":6}')

        ttl = await contract_redis.ttl(f"gwevent:v1:{match_id}")
        assert 0 < ttl <= 3600
