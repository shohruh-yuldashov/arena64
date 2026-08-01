"""The `friends:v1:` cache — A64-013.6.

Three layers, and each one is asserted where its decisions actually live:

    TestTheKeyspace          that the two entries are namespaced and
                             distinct, and that `keys_for` names every one
    TestTheRedisAdapter      what `RedisSocialGraphCache` stores, and what
                             it does when Redis misbehaves
    TestTheCachedReader      that a hit costs no query and a miss costs
                             exactly one

A64-013.6 asks for three cache tests — `blocked_ids`, `friend_ids`, and
invalidation. The first two are here; invalidation is asserted at both ends:
the adapter's `DEL` below, and the four *triggers* end-to-end in
`tests/contract/test_social_graph_cache_api.py`, because "blocking
invalidates the cache" is a claim about `BlockingService` and not about
Redis.

Runs the **real** adapter and the **real** decorator throughout, over fakes
of Redis and of the repositories. Substituting either would leave the JSON
encoding, the key derivation and the query-count promises untested — which
is where a cache's bugs are.
"""

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis

from app.config.settings import FriendsSettings
from app.modules.friends.application.services.cached_social_graph_reader import (
    CachedSocialGraphReader,
)
from app.modules.friends.application.services.social_graph_reader import (
    SocialGraphReaderService,
)
from app.modules.friends.infrastructure.cache import (
    KEY_VERSION,
    NoSocialGraphCache,
    RedisSocialGraphCache,
    blocked_ids_key,
    friend_ids_key,
    keys_for,
)
from tests.fakes.social_graph_cache import (
    FakeCacheRedis,
    RecordingSocialGraphCache,
    UnreachableCacheRedis,
)

VIEWER = UUID("019fbb2c-7d41-7f0a-9b31-2c5e8a1f4d60")
FRIEND = UUID("019fbb2c-8e52-7b1c-8c42-3d6f9b2a5e71")
STRANGER = UUID("019fbb2c-9f63-7c2d-9d53-4e7a0c3b6f82")
BLOCKED = UUID("019fbb2d-0a74-7d3e-8e64-5f8b1d4c7a93")


class _CountingFriendshipRepository:
    """Answers the two friendship reads and counts every one.

    Only the methods `SocialGraphReaderService` calls, deliberately: a fake
    that implemented the whole repository would be a second implementation
    to keep in step with the first (RP-05), and nothing here exercises the
    rest of it.
    """

    def __init__(self, friends: set[UUID]) -> None:
        self._friends = friends
        self.among_calls = 0
        self.all_calls = 0

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        self.among_calls += 1
        return {other for other in others if other in self._friends}

    async def friend_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        self.all_calls += 1
        return frozenset(self._friends)


class _CountingBlockRepository:
    def __init__(self, blocked: set[UUID]) -> None:
        self._blocked = blocked
        self.calls = 0

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        self.calls += 1
        return frozenset(self._blocked)


@pytest.fixture
def friendships() -> _CountingFriendshipRepository:
    return _CountingFriendshipRepository({FRIEND})


@pytest.fixture
def blocks() -> _CountingBlockRepository:
    return _CountingBlockRepository({BLOCKED})


@pytest.fixture
def cache() -> RecordingSocialGraphCache:
    return RecordingSocialGraphCache()


@pytest.fixture
def reader(
    friendships: _CountingFriendshipRepository,
    blocks: _CountingBlockRepository,
    cache: RecordingSocialGraphCache,
) -> CachedSocialGraphReader:
    return CachedSocialGraphReader(
        SocialGraphReaderService(
            friendships=cast(Any, friendships),
            blocks=cast(Any, blocks),
        ),
        cache,
    )


class TestTheKeyspace:
    def test_every_key_lives_under_the_reserved_namespace(self) -> None:
        """A64-013.1 reserved `friends:v1:` and A64-013.6 is the first task
        to write it. Nothing may land outside the reservation — caching.md
        C-2 makes the version segment the migration mechanism, and a key
        without it cannot be rolled."""
        for key in keys_for(VIEWER):
            assert key.startswith(f"friends:{KEY_VERSION}:")

    def test_the_two_entries_do_not_collide(self) -> None:
        """Friends and blocks are different sets about the same player, and
        one key holding either would be the worst cache bug available: a
        block list served as a friend list."""
        assert friend_ids_key(VIEWER) != blocked_ids_key(VIEWER)

    def test_a_key_is_per_player(self) -> None:
        assert friend_ids_key(VIEWER) != friend_ids_key(STRANGER)

    def test_keys_for_names_every_entry_in_the_namespace(self) -> None:
        """The invalidation contract, structurally.

        `invalidate` drops exactly what `keys_for` returns, so an entry
        added to the namespace and not to `keys_for` would never be dropped
        — stale for a whole TTL, and invisible until somebody noticed a
        removed friend still listed.
        """
        assert set(keys_for(VIEWER)) == {friend_ids_key(VIEWER), blocked_ids_key(VIEWER)}


class TestTheRedisAdapter:
    @pytest.fixture
    def redis(self) -> FakeCacheRedis:
        return FakeCacheRedis()

    @pytest.fixture
    def adapter(self, redis: FakeCacheRedis) -> RedisSocialGraphCache:
        return RedisSocialGraphCache(cast(Redis, redis), settings=FriendsSettings())

    async def test_a_stored_set_reads_back_whole(self, adapter: RedisSocialGraphCache) -> None:
        await adapter.put_ids(friend_ids_key(VIEWER), frozenset({FRIEND, STRANGER}))

        assert await adapter.get_ids(friend_ids_key(VIEWER)) == frozenset({FRIEND, STRANGER})

    async def test_an_empty_set_is_a_hit_and_not_a_miss(
        self, adapter: RedisSocialGraphCache
    ) -> None:
        """The reason values are JSON arrays rather than Redis sets.

        A player with no friends is the most common state on this platform.
        Stored as a Redis `SET`, "no friends" and "not cached" would be the
        same thing, and those players would miss on every single read —
        the cache would be off for exactly the majority case.
        """
        await adapter.put_ids(friend_ids_key(VIEWER), frozenset())

        assert await adapter.get_ids(friend_ids_key(VIEWER)) == frozenset()

    async def test_an_absent_key_misses(self, adapter: RedisSocialGraphCache) -> None:
        assert await adapter.get_ids(friend_ids_key(VIEWER)) is None

    async def test_nothing_is_ever_stored_without_an_expiry(
        self, adapter: RedisSocialGraphCache, redis: FakeCacheRedis
    ) -> None:
        """The TTL is the backstop for invalidation failing (caching.md
        C-3). A key written without one would be stale forever if a trigger
        ever missed it, which is the failure the backstop exists for."""
        await adapter.put_ids(blocked_ids_key(VIEWER), frozenset({BLOCKED}))

        assert redis.expiries[blocked_ids_key(VIEWER)] == FriendsSettings().cache_ttl_seconds

    async def test_invalidating_a_player_drops_both_of_their_entries(
        self, adapter: RedisSocialGraphCache
    ) -> None:
        """One call drops the whole namespace for that player.

        Blocking changes a block set *and* ends a friendship, so an
        invalidation that dropped only the entry its trigger was named after
        would leave the other stale.
        """
        await adapter.put_ids(friend_ids_key(VIEWER), frozenset({FRIEND}))
        await adapter.put_ids(blocked_ids_key(VIEWER), frozenset({BLOCKED}))

        await adapter.invalidate([VIEWER])

        assert await adapter.get_ids(friend_ids_key(VIEWER)) is None
        assert await adapter.get_ids(blocked_ids_key(VIEWER)) is None

    async def test_invalidating_a_pair_drops_both_players(
        self, adapter: RedisSocialGraphCache
    ) -> None:
        """A friendship and a block are facts about a pair, so both sides
        are dropped — a viewer whose own entry survived would still see the
        friend they just removed."""
        await adapter.put_ids(friend_ids_key(VIEWER), frozenset({FRIEND}))
        await adapter.put_ids(friend_ids_key(FRIEND), frozenset({VIEWER}))

        await adapter.invalidate([VIEWER, FRIEND])

        assert await adapter.get_ids(friend_ids_key(VIEWER)) is None
        assert await adapter.get_ids(friend_ids_key(FRIEND)) is None

    async def test_invalidating_nobody_issues_no_command(
        self, adapter: RedisSocialGraphCache, redis: FakeCacheRedis
    ) -> None:
        """`DEL` with no keys is an error in Redis, so the empty case is
        guarded rather than sent."""
        redis.values["untouched"] = "[]"

        await adapter.invalidate([])

        assert redis.values == {"untouched": "[]"}

    async def test_an_unreachable_cache_reads_as_a_miss(self) -> None:
        """Never raises: a profile render must survive Redis being down,
        because the database still has every one of these answers."""
        adapter = RedisSocialGraphCache(
            cast(Redis, UnreachableCacheRedis()), settings=FriendsSettings()
        )

        assert await adapter.get_ids(friend_ids_key(VIEWER)) is None

    async def test_an_unreachable_cache_swallows_writes_and_invalidations(self) -> None:
        """The invalidation failure is logged at `ERROR` and still does not
        raise — a block that failed to invalidate must not also fail to be
        placed, because the database is the system of record."""
        adapter = RedisSocialGraphCache(
            cast(Redis, UnreachableCacheRedis()), settings=FriendsSettings()
        )

        await adapter.put_ids(friend_ids_key(VIEWER), frozenset({FRIEND}))
        await adapter.invalidate([VIEWER, FRIEND])

    async def test_a_malformed_value_is_ignored_rather_than_decoded(
        self, adapter: RedisSocialGraphCache, redis: FakeCacheRedis
    ) -> None:
        """Something other than this code wrote the key. The safe answer is
        to miss, not to decode half a social graph."""
        redis.values[friend_ids_key(VIEWER)] = "{not json"

        assert await adapter.get_ids(friend_ids_key(VIEWER)) is None

    async def test_a_value_holding_something_that_is_not_an_id_misses(
        self, adapter: RedisSocialGraphCache, redis: FakeCacheRedis
    ) -> None:
        redis.values[friend_ids_key(VIEWER)] = '["not-a-uuid"]'

        assert await adapter.get_ids(friend_ids_key(VIEWER)) is None


class TestTheCachedReader:
    async def test_the_blocked_set_is_read_once_and_then_served_from_cache(
        self, reader: CachedSocialGraphReader, blocks: _CountingBlockRepository
    ) -> None:
        """`blocked_ids_for` is the hottest read on the platform — every
        composition and every search — and A64-013.6 asks for it to be
        optimised. One query, then none."""
        first = await reader.blocked_ids_for(VIEWER)
        second = await reader.blocked_ids_for(VIEWER)

        assert first == second == frozenset({BLOCKED})
        assert blocks.calls == 1

    async def test_the_friend_set_is_read_once_and_then_intersected_in_memory(
        self,
        reader: CachedSocialGraphReader,
        friendships: _CountingFriendshipRepository,
    ) -> None:
        """One key per player answers a page of any length and any contents,
        which is why the whole set is cached rather than the query."""
        first = await reader.friend_ids_among(VIEWER, [FRIEND, STRANGER])
        second = await reader.friend_ids_among(VIEWER, [STRANGER, BLOCKED, FRIEND])

        assert first == {FRIEND}
        assert second == {FRIEND}
        assert friendships.all_calls == 1

    async def test_a_miss_costs_exactly_one_query(
        self,
        reader: CachedSocialGraphReader,
        friendships: _CountingFriendshipRepository,
    ) -> None:
        """The uncached reader costs one query too, so an inert or cold
        cache is never a regression. An earlier shape answered the narrow
        question and *then* read the whole set to populate the entry — two
        queries per miss, and two on every request with the cache off."""
        await reader.friend_ids_among(VIEWER, [FRIEND])

        assert friendships.all_calls + friendships.among_calls == 1

    async def test_an_empty_page_asks_nothing_of_anybody(
        self,
        reader: CachedSocialGraphReader,
        friendships: _CountingFriendshipRepository,
        cache: RecordingSocialGraphCache,
    ) -> None:
        """A search nobody matched is the ordinary case, and it is neither a
        query nor a cache read."""
        assert await reader.friend_ids_among(VIEWER, []) == set()
        assert friendships.all_calls == 0
        assert cache.reads == []

    async def test_an_invalidated_entry_is_re_read_from_the_database(
        self,
        reader: CachedSocialGraphReader,
        friendships: _CountingFriendshipRepository,
        cache: RecordingSocialGraphCache,
    ) -> None:
        """The whole invalidation contract in one assertion: after the
        cache is dropped, the *next* read goes back to the system of
        record rather than serving what it had."""
        await reader.friend_ids_among(VIEWER, [FRIEND])
        await cache.invalidate([VIEWER])

        await reader.friend_ids_among(VIEWER, [FRIEND])

        assert friendships.all_calls == 2

    async def test_a_cache_that_never_stores_still_returns_the_right_answer(
        self,
        friendships: _CountingFriendshipRepository,
        blocks: _CountingBlockRepository,
    ) -> None:
        """`FRIENDS_CACHE_ENABLED=false`, end to end.

        The kill switch must change performance and nothing else — the
        reader is the same object, so a bug here would be a wrong answer
        rather than a slow one.
        """
        reader = CachedSocialGraphReader(
            SocialGraphReaderService(friendships=cast(Any, friendships), blocks=cast(Any, blocks)),
            NoSocialGraphCache(),
        )

        assert await reader.friend_ids_among(VIEWER, [FRIEND, STRANGER]) == {FRIEND}
        assert await reader.blocked_ids_for(VIEWER) == frozenset({BLOCKED})
        assert await reader.friend_ids_among(VIEWER, [FRIEND]) == {FRIEND}

    async def test_an_unknown_viewer_caches_their_empty_graph(
        self, cache: RecordingSocialGraphCache
    ) -> None:
        """A player with no friends and no blocks is cached as empty rather
        than left to miss forever — the majority case, and the one a Redis
        `SET` would have got wrong."""
        empty = CachedSocialGraphReader(
            SocialGraphReaderService(
                friendships=cast(Any, _CountingFriendshipRepository(set())),
                blocks=cast(Any, _CountingBlockRepository(set())),
            ),
            cache,
        )

        await empty.friend_ids_among(uuid4(), [FRIEND])

        assert frozenset() in cache.entries.values()
