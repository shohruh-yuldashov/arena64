"""Social presence end to end — A64-013.6.

Two things this task added that only a full stack can prove, and neither is
provable from a unit test:

  **The presence producer exists.** A64-012.7 left `PresenceRecorder` with no
  writer at all. The claim being checked is that *signing in* now records
  presence and *signing out of everywhere* records absence — a claim about
  `auth`'s routes, the composition root and the recorder together.

  **The four invalidation triggers fire.** `friend accepted`, `friend
  removed`, `player blocked`, `player unblocked` — each one is a statement
  about a service committing a transaction and then dropping two players'
  cache entries. `tests/unit/test_social_graph_cache.py` asserts that
  dropping the entries works; this asserts that the four writes actually do
  it, through HTTP, against real PostgreSQL.

The cache here is `RecordingSocialGraphCache` rather than the Redis adapter,
and that is the same line `build_contract_app` draws everywhere else: Redis
is infrastructure the test environment should not need, and *which* entries
a trigger drops is a property of this code. The entries it asserts against
are `SocialGraphEntry` members, which is the vocabulary the port itself
uses since A64-013.8 — so an entry added without an invalidation trigger
fails here too.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import PresenceSettings
from app.modules.friends.application.ports import SocialGraphEntry
from app.modules.users.infrastructure.presence import RedisPresenceProvider
from tests.contract.contract_app import build_contract_app, contract_client
from tests.fakes.presence_redis import (
    NOW,
    FakePresenceRedis,
    MovableClock,
    UnreachablePresenceRedis,
)
from tests.fakes.social_graph_cache import RecordingSocialGraphCache

BLOCKS_URL = "/api/v1/blocks"
FRIENDS_URL = "/api/v1/friends"
REQUESTS_URL = f"{FRIENDS_URL}/requests"
SEARCH_URL = "/api/v1/users/search"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
LOGOUT_URL = "/api/v1/auth/logout"
LOGOUT_ALL_URL = "/api/v1/auth/logout-all"
REFRESH_URL = "/api/v1/auth/refresh"
PASSWORD = "CorrectHorse1!"


class Player:
    def __init__(self, player_id: UUID, username: str, tokens: dict[str, str]) -> None:
        self.id = player_id
        self.username = username
        self.tokens = tokens

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens['access_token']}"}

    @property
    def refresh_token(self) -> str:
        return self.tokens["refresh_token"]


class _Fixtures:
    """The clock, the presence store and the cache a test can look inside.

    Bundled rather than exposed as four fixtures because every test that
    wants one wants the app built around it, and the app has to be built
    once — a second `build_contract_app` would wire a second cache and the
    assertions would read the wrong one.
    """

    def __init__(self) -> None:
        self.clock = MovableClock(NOW)
        self.redis = FakePresenceRedis(self.clock)
        self.presence = RedisPresenceProvider(
            self.redis,  # type: ignore[arg-type]
            settings=PresenceSettings(ttl_seconds=60),
            clock=self.clock,
        )
        self.cache = RecordingSocialGraphCache()


@pytest_asyncio.fixture
async def stack(contract_session: AsyncSession) -> AsyncIterator[tuple[AsyncClient, _Fixtures]]:
    """The production app with presence and the cache both switched **on**.

    Every other contract suite runs on `NoPresenceProvider` and
    `NoSocialGraphCache`, which is what `PRESENCE_ENABLED=false` and
    `FRIENDS_CACHE_ENABLED=false` wire in production. This file is the one
    that needs both live, so it is the one that passes them.

    The same object is handed to the recorder and the provider, exactly as
    `users.presentation.dependencies` does — so what a test reads back is
    what a route wrote.
    """
    fixtures = _Fixtures()
    app = build_contract_app(
        contract_session,
        presence=fixtures.presence,
        presence_recorder=fixtures.presence,
        social_graph_cache=fixtures.cache,
    )
    async with contract_client(app) as http:
        yield http, fixtures


async def register(client: AsyncClient, session: AsyncSession) -> Player:
    suffix = uuid4().hex[:8]
    username = f"player{suffix}"
    assert len(username) <= 20, f"test username {username!r} exceeds the platform limit"

    created = await client.post(
        REGISTER_URL,
        json={"username": username, "email": f"{suffix}@example.com", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text

    # **Verified**, because A64-021.5H put this write behind a verified
    # address. The same thing `app.operator.accounts verify` does; the OTP
    # flow itself belongs to `test_otp_verification.py`.
    await session.execute(
        text("UPDATE users.user SET is_verified = true WHERE id = :id"),
        {"id": UUID(created.json()["data"]["id"])},
    )

    signed_in = await client.post(
        LOGIN_URL, json={"email": f"{suffix}@example.com", "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    return Player(UUID(created.json()["data"]["id"]), username, signed_in.json()["data"])


async def befriend(client: AsyncClient, a: Player, b: Player) -> None:
    sent = await client.post(REQUESTS_URL, headers=a.auth, json={"player_id": str(b.id)})
    assert sent.status_code == 201, sent.text
    accepted = await client.post(
        f"{REQUESTS_URL}/{sent.json()['data']['id']}/accept", headers=b.auth
    )
    assert accepted.status_code == 200, accepted.text


class TestThePresenceProducer:
    async def test_signing_in_records_presence(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """The missing infrastructure, present.

        `register` signs in, so by the time it returns the player must be
        online — with no gateway, no socket and no client cooperation.
        """
        client, fixtures = stack

        player = await register(client, contract_session)

        recorded = await fixtures.presence.presence_for(player.id)
        assert recorded is not None
        assert recorded.is_online is True

    async def test_a_player_who_never_signed_in_has_no_presence(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """Nothing else on the platform writes presence, so an account that
        exists and has not signed in is absent rather than offline."""
        client, fixtures = stack
        created = await client.post(
            REGISTER_URL,
            json={
                "username": f"quiet{uuid4().hex[:8]}",
                "email": f"{uuid4().hex[:8]}@example.com",
                "password": PASSWORD,
            },
        )
        assert created.status_code == 201, created.text

        assert await fixtures.presence.presence_for(UUID(created.json()["data"]["id"])) is None

    async def test_refreshing_a_token_keeps_the_player_online(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """The liveness protocol through the API.

        The presence window is shorter than a session, so a signed-in
        player who never refreshed would go dark. Refreshing restarts it —
        which is what makes "still at the keyboard" observable without a
        socket.
        """
        client, fixtures = stack
        player = await register(client, contract_session)

        fixtures.clock.advance(59)
        refreshed = await client.post(REFRESH_URL, json={"refresh_token": player.refresh_token})
        assert refreshed.status_code == 200, refreshed.text

        fixtures.clock.advance(30)
        still_here = await fixtures.presence.presence_for(player.id)
        assert still_here is not None
        assert still_here.is_online is True

    async def test_presence_lapses_when_nothing_refreshes_it(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """A player who closes the tab stops being online without anything
        observing that they left. Nothing else could: there is no
        connection to drop."""
        client, fixtures = stack
        player = await register(client, contract_session)

        fixtures.clock.advance(61)

        assert await fixtures.presence.presence_for(player.id) is None

    async def test_signing_out_everywhere_records_absence(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """`POST /auth/logout-all` revokes every session, so there is no
        device left that could be present."""
        client, fixtures = stack
        player = await register(client, contract_session)

        signed_out = await client.post(LOGOUT_ALL_URL, headers=player.auth)
        assert signed_out.status_code == 204, signed_out.text

        recorded = await fixtures.presence.presence_for(player.id)
        assert recorded is not None
        assert recorded.is_online is False

    async def test_signing_out_of_one_device_does_not_record_absence(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """Presence is per **player**, not per session.

        A player signing out on a laptop may still be signed in on a phone,
        and publishing "offline" would be a falsehood the phone's next
        refresh silently corrects. The deliberate omission, asserted so it
        cannot be "fixed" by accident.
        """
        client, fixtures = stack
        player = await register(client, contract_session)

        signed_out = await client.post(LOGOUT_URL, json={"refresh_token": player.refresh_token})
        assert signed_out.status_code == 204, signed_out.text

        recorded = await fixtures.presence.presence_for(player.id)
        assert recorded is not None
        assert recorded.is_online is True

    async def test_signing_in_never_fails_because_presence_did(
        self, contract_session: AsyncSession
    ) -> None:
        """A sign-in must not depend on Redis.

        Built on the *unreachable* store rather than the working one, so
        every presence write raises inside the adapter. `register` asserts
        `201` and `200` itself, which is the whole assertion — the player
        is simply not recorded as online.
        """
        broken = RedisPresenceProvider(
            UnreachablePresenceRedis(),  # type: ignore[arg-type]
            settings=PresenceSettings(ttl_seconds=60),
            clock=MovableClock(NOW),
        )
        app = build_contract_app(contract_session, presence=broken, presence_recorder=broken)
        async with contract_client(app) as http:
            player = await register(http, contract_session)

            assert await broken.presence_for(player.id) is None


class TestCacheInvalidation:
    """The four triggers A64-013.6 names, one test each.

    Each asserts that **both** parties' entries are gone, because a
    friendship and a block are facts about a pair — an invalidation that
    dropped only the actor's entry would leave the other side reading a
    friend it no longer has.
    """

    async def test_accepting_a_request_invalidates_both_players(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """Acceptance is the only way a friendship comes into existence, so
        it is the only friend-request transition that touches the cache.

        Asserted on the invalidation *and* on what survives it, because the
        accept response composes the new friend's profile — which repopulates
        the accepter's entry inside the same request, from the committed row.
        A stale value could therefore only survive as the empty set seeded
        below, and neither key holds it afterwards. That repopulation is the
        design working: the entry is rebuilt after the commit, never before
        it (see `FriendRequestService._transition`).
        """
        client, fixtures = stack
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        fixtures.cache.entries[(alice.id, SocialGraphEntry.FRIENDS)] = frozenset()
        fixtures.cache.entries[(bob.id, SocialGraphEntry.FRIENDS)] = frozenset()

        await befriend(client, alice, bob)

        assert set(fixtures.cache.invalidations[-1]) == {alice.id, bob.id}
        assert fixtures.cache.entries.get((alice.id, SocialGraphEntry.FRIENDS)) in (
            None,
            frozenset({bob.id}),
        )
        assert fixtures.cache.entries.get((bob.id, SocialGraphEntry.FRIENDS)) in (
            None,
            frozenset({alice.id}),
        )

    async def test_sending_a_request_invalidates_nothing(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """Only *acceptance* changes the graph.

        A pending request is not a friendship, and invalidating on send
        would throw away a hot entry for an event that changed nothing —
        every unanswered request would cost two players their cache.
        """
        client, fixtures = stack
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        fixtures.cache.invalidations.clear()

        sent = await client.post(REQUESTS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        assert sent.status_code == 201, sent.text

        assert fixtures.cache.invalidations == []

    async def test_declining_a_request_invalidates_nothing(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """A declined request leaves the graph exactly as it was."""
        client, fixtures = stack
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        sent = await client.post(REQUESTS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        assert sent.status_code == 201, sent.text
        fixtures.cache.invalidations.clear()

        declined = await client.post(
            f"{REQUESTS_URL}/{sent.json()['data']['id']}/decline", headers=bob.auth
        )
        assert declined.status_code == 200, declined.text

        assert fixtures.cache.invalidations == []

    async def test_removing_a_friend_invalidates_both_players(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        client, fixtures = stack
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        await befriend(client, alice, bob)
        fixtures.cache.entries[(alice.id, SocialGraphEntry.FRIENDS)] = frozenset({bob.id})
        fixtures.cache.entries[(bob.id, SocialGraphEntry.FRIENDS)] = frozenset({alice.id})

        removed = await client.delete(f"{FRIENDS_URL}/{bob.id}", headers=alice.auth)
        assert removed.status_code == 204, removed.text

        assert (alice.id, SocialGraphEntry.FRIENDS) not in fixtures.cache.entries
        assert (bob.id, SocialGraphEntry.FRIENDS) not in fixtures.cache.entries

    async def test_blocking_invalidates_both_players(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """Blocking touches both entries of both players: the block sets
        change, and the cascade ends any friendship (FS-3)."""
        client, fixtures = stack
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        await befriend(client, alice, bob)
        for player in (alice, bob):
            fixtures.cache.entries[(player.id, SocialGraphEntry.FRIENDS)] = frozenset()
            fixtures.cache.entries[(player.id, SocialGraphEntry.BLOCKED)] = frozenset()

        blocked = await client.post(BLOCKS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        assert blocked.status_code == 201, blocked.text

        assert fixtures.cache.entries == {}

    async def test_unblocking_invalidates_both_players(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """The trigger that would be *silently* wrong if it were missing:
        a lifted block that stayed cached is a player still invisible to
        somebody who deliberately unblocked them."""
        client, fixtures = stack
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        blocked = await client.post(BLOCKS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        assert blocked.status_code == 201, blocked.text
        fixtures.cache.entries[(alice.id, SocialGraphEntry.BLOCKED)] = frozenset({bob.id})
        fixtures.cache.entries[(bob.id, SocialGraphEntry.BLOCKED)] = frozenset({alice.id})

        lifted = await client.delete(f"{BLOCKS_URL}/{bob.id}", headers=alice.auth)
        assert lifted.status_code == 204, lifted.text

        assert (alice.id, SocialGraphEntry.BLOCKED) not in fixtures.cache.entries
        assert (bob.id, SocialGraphEntry.BLOCKED) not in fixtures.cache.entries

    async def test_a_stale_block_set_cannot_outlive_the_unblock(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """The end-to-end version of the rule: never leave stale
        friendship state.

        Warms the cache through a real read, lifts the block, and reads
        again — the second read must reflect the database rather than what
        Redis was holding.
        """
        client, fixtures = stack
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        await client.post(BLOCKS_URL, headers=alice.auth, json={"player_id": str(bob.id)})

        # A search warms `blocked_ids_for` through the composition path.
        hidden = await client.get(SEARCH_URL, headers=alice.auth, params={"q": bob.username})
        assert [item["username"] for item in hidden.json()["data"]["items"]] == []

        await client.delete(f"{BLOCKS_URL}/{bob.id}", headers=alice.auth)

        visible = await client.get(SEARCH_URL, headers=alice.auth, params={"q": bob.username})
        assert [item["username"] for item in visible.json()["data"]["items"]] == [bob.username]


class TestCachedReads:
    async def test_the_block_set_is_read_from_the_database_once(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """`blocked_ids_for` is the hottest read this task touches, and the
        point of caching it is that the second request does not issue it.

        Counted by relation name against the real engine, because a cache
        that returned the right answer while still querying would pass any
        assertion made on the response.
        """
        client, fixtures = stack
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        await client.post(BLOCKS_URL, headers=alice.auth, json={"player_id": str(bob.id)})

        statements: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = contract_session.get_bind().engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            await client.get(SEARCH_URL, headers=alice.auth, params={"q": "player"})
            first = _block_reads(statements)
            await client.get(SEARCH_URL, headers=alice.auth, params={"q": "player"})
            second = _block_reads(statements) - first
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert first == 1, "the first search should populate the block set"
        assert second == 0, "the second search should be served from the cache"

    async def test_a_page_of_friends_costs_one_graph_read_however_long_it_is(
        self, stack: tuple[AsyncClient, _Fixtures], contract_session: AsyncSession
    ) -> None:
        """No N+1, and batched.

        Composing a page asks the social graph a **fixed** number of times
        for the whole page, not once per player — the property A64-013.4
        established and neither the cache in front of it nor A64-020.4's
        published relationship may regress.

        Asserted as "the same for one row as for many" rather than as an
        absolute count. A64-020.4 changed the absolute from one to two by
        adding a second, genuinely different question — privacy asks who is
        a friend, the published field asks what the viewer may do — and a
        pinned number would have failed for correct behaviour, with the
        tempting fix being to raise it rather than ask what produced it.
        """
        client, fixtures = stack
        alice = await register(client, contract_session)
        for _ in range(3):
            await befriend(client, alice, await register(client, contract_session))

        statements: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = contract_session.get_bind().engine

        async def graph_reads(limit: int) -> tuple[int, int]:
            statements.clear()
            event.listen(engine, "before_cursor_execute", record)
            try:
                listed = await client.get(
                    SEARCH_URL, headers=alice.auth, params={"q": "player", "limit": limit}
                )
            finally:
                event.remove(engine, "before_cursor_execute", record)
            assert listed.status_code == 200, listed.text
            return _friendship_reads(statements), len(listed.json()["data"]["items"])

        # Warmed first. `CachedSocialGraphReader` populates the block set on
        # the first search of a session, so a cold measurement and a warm one
        # differ by that read and are not comparable — which is exactly the
        # trap a single-request assertion avoided and a two-request one has
        # to handle explicitly.
        await graph_reads(1)

        one_read, one_row = await graph_reads(1)
        many_reads, many_rows = await graph_reads(20)

        assert one_row == 1
        assert many_rows > one_row, "the fixture should produce more than one match"
        assert one_read >= 1, "search must resolve the graph at all"
        assert one_read == many_reads, (
            f"the graph was read {one_read} times for one row and {many_reads} for "
            f"{many_rows} — the resolution is per player"
        )


def _block_reads(statements: list[str]) -> int:
    return len([s for s in statements if "blocked_player.blocker_id" in s and "SELECT" in s])


def _friendship_reads(statements: list[str]) -> int:
    return len(
        [
            s
            for s in statements
            if "friendship.player_low_id" in s
            and "friendship.player_high_id" in s
            and "count" not in s.lower()
            and "friendship.id" not in s
        ]
    )
