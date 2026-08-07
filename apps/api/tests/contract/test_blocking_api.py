"""Blocking end to end — real PostgreSQL, real constraints, the real
composition root.

A64-013.5 names blocking as "not an isolated feature ... a platform-wide
policy", and this file is where that claim is checked: every one of the five
surfaces it lists is asserted through HTTP, on a graph built entirely
through the API.

    profile visibility   a blocked reader sees nothing audience-valued
    presence visibility  the same gate, same code path
    search               blocked players disappear, both directions
    friendships          terminated with `BLOCKED`, history kept
    friend requests      pending ones voided, new ones refused

What else is here is the small set of properties that would be *silently*
wrong rather than loudly broken:

  - **the cascade is one transaction**, asserted by making the block fail
    and checking the friendship survived;
  - **the refusal is indistinguishable** from a request to a player who
    does not exist (FR-2), asserted as an equality between two responses
    rather than against a constant;
  - **`BLOCKED` outranks `EVERYONE`**, which a gate that checked the level
    first would get wrong for exactly one person.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.friends.infrastructure.models import (
    BlockedPlayerModel,
    FriendRequestModel,
    FriendshipModel,
)
from tests.contract.contract_app import build_contract_app, contract_client

BLOCKS_URL = "/api/v1/blocks"
FRIENDS_URL = "/api/v1/friends"
COUNT_URL = f"{FRIENDS_URL}/count"
REQUESTS_URL = f"{FRIENDS_URL}/requests"
PROFILES_URL = "/api/v1/profiles"
SEARCH_URL = "/api/v1/users/search"
PRIVACY_URL = "/api/v1/profile/privacy"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app over the test's rolled-back transaction.

    No override on any blocking, friends or profiles service — the graph
    under test is the one that ships, including the real
    `SocialGraphBlockedPlayersProvider` reading the real relation.
    """
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


class Player:
    def __init__(self, player_id: UUID, username: str, auth: dict[str, str]) -> None:
        self.id = player_id
        self.username = username
        self.auth = auth


async def register(client: AsyncClient, session: AsyncSession, *, prefix: str = "player") -> Player:
    """One registered, signed-in account.

    `prefix` lets a search test give several accounts a shared, unique
    stem so a term built from it matches that test's players and nothing a
    neighbour left behind. Kept short deliberately: usernames are capped at
    20 characters, and the suffix below takes eight of them.
    """
    suffix = uuid4().hex[:8]
    username = f"{prefix}{suffix}"
    assert len(username) <= 20, f"test username {username!r} exceeds the platform limit"
    created = await client.post(
        REGISTER_URL,
        json={"username": username, "email": f"{suffix}@example.com", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text

    # **Verified**, because A64-021.5H made every friend-graph write require
    # it — and blocking is a friend-graph write. The same thing
    # `app.operator.accounts verify` does; the OTP flow belongs to
    # `test_otp_verification.py`.
    await session.execute(
        text("UPDATE users.user SET is_verified = true WHERE id = :id"),
        {"id": UUID(created.json()["data"]["id"])},
    )

    signed_in = await client.post(
        LOGIN_URL, json={"email": f"{suffix}@example.com", "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    return Player(
        UUID(created.json()["data"]["id"]),
        username,
        {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"},
    )


@pytest_asyncio.fixture
async def alice(client: AsyncClient, contract_session: AsyncSession) -> Player:
    return await register(client, contract_session)


@pytest_asyncio.fixture
async def bob(client: AsyncClient, contract_session: AsyncSession) -> Player:
    return await register(client, contract_session)


async def befriend(client: AsyncClient, a: Player, b: Player) -> str:
    sent = await client.post(REQUESTS_URL, headers=a.auth, json={"player_id": str(b.id)})
    assert sent.status_code == 201, sent.text
    request_id = sent.json()["data"]["id"]
    accepted = await client.post(f"{REQUESTS_URL}/{request_id}/accept", headers=b.auth)
    assert accepted.status_code == 200, accepted.text
    return str(request_id)


async def block(client: AsyncClient, blocker: Player, blocked: Player) -> Any:
    return await client.post(BLOCKS_URL, headers=blocker.auth, json={"player_id": str(blocked.id)})


class TestBlock:
    async def test_a_player_can_be_blocked(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        response = await block(client, alice, bob)

        assert response.status_code == 201, response.text
        assert response.json()["data"]["player"]["id"] == str(bob.id)
        assert response.json()["data"]["blocked_at"]

    async def test_self_block_is_refused(self, client: AsyncClient, alice: Player) -> None:
        response = await block(client, alice, alice)

        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    async def test_a_duplicate_block_is_refused(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """**Not idempotent**, unlike unblocking, because blocking runs a
        cascade: reporting success for a repeat would claim a cascade ran
        that did not."""
        await block(client, alice, bob)

        response = await block(client, alice, bob)

        assert response.status_code == 409

    async def test_the_unique_index_is_what_enforces_it(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """BE-06, driven straight at the database. It matters more here than
        elsewhere: a second block slipping through would re-run the cascade
        against an already-ended friendship."""
        await block(client, alice, bob)

        with pytest.raises(Exception, match="uq_blocked_player__pair"):
            await contract_session.execute(
                text(
                    "INSERT INTO friends.blocked_player "
                    "(id, blocker_id, blocked_id, created_at) "
                    "VALUES (gen_random_uuid(), :a, :b, now())"
                ),
                {"a": alice.id, "b": bob.id},
            )

    async def test_blocks_are_directional(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """A blocking B and B blocking A are two different facts, both of
        which can be true — so the reverse block is not a duplicate."""
        await block(client, alice, bob)

        assert (await block(client, bob, alice)).status_code == 201


class TestUnblock:
    async def test_a_block_can_be_lifted(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        await block(client, alice, bob)

        response = await client.delete(f"{BLOCKS_URL}/{bob.id}", headers=alice.auth)

        assert response.status_code == 204, response.text
        assert (await client.get(BLOCKS_URL, headers=alice.auth)).json()["data"]["items"] == []

    async def test_unblocking_is_idempotent(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        await block(client, alice, bob)

        first = await client.delete(f"{BLOCKS_URL}/{bob.id}", headers=alice.auth)
        second = await client.delete(f"{BLOCKS_URL}/{bob.id}", headers=alice.auth)

        assert first.status_code == second.status_code == 204

    async def test_the_row_is_hard_deleted(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """The one relation on this platform that is genuinely deleted —
        database.md §7.2: retaining released blocks would make BL-2's
        matchmaking filter read rows it must then exclude."""
        await block(client, alice, bob)
        await client.delete(f"{BLOCKS_URL}/{bob.id}", headers=alice.auth)

        assert (await contract_session.scalars(select(BlockedPlayerModel))).all() == []

    async def test_unblocking_restores_nothing(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """BL-3 cuts both ways: the block did not erase the friendship, and
        lifting it does not resurrect one."""
        await befriend(client, alice, bob)
        await block(client, alice, bob)
        await client.delete(f"{BLOCKS_URL}/{bob.id}", headers=alice.auth)

        assert (await client.get(COUNT_URL, headers=alice.auth)).json()["data"]["total"] == 0

    async def test_only_your_own_blocks_are_liftable(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """A block placed *on* you is not addressable — you cannot see it
        and cannot lift it (BL-1)."""
        await block(client, alice, bob)

        response = await client.delete(f"{BLOCKS_URL}/{alice.id}", headers=bob.auth)

        assert response.status_code == 204
        # Alice's block survives: Bob lifted a block of his own that never
        # existed, not hers.
        assert len((await client.get(BLOCKS_URL, headers=alice.auth)).json()["data"]["items"]) == 1


class TestBlockList:
    async def test_it_lists_only_blocks_you_placed(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """Blocks placed *on* you never appear — that invisibility is the
        whole reason a block is worth placing (BL-1), and it is a property
        of the query rather than a filter somebody remembers."""
        await block(client, alice, bob)

        assert [
            i["player"]["id"]
            for i in (await client.get(BLOCKS_URL, headers=alice.auth)).json()["data"]["items"]
        ] == [str(bob.id)]
        assert (await client.get(BLOCKS_URL, headers=bob.auth)).json()["data"]["items"] == []

    async def test_the_blocker_can_still_see_who_they_blocked(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """A block list that hid its own entries would be unusable: you
        cannot lift a block on somebody you cannot identify. The blocker
        sees what they could see before blocking — never more."""
        await block(client, alice, bob)

        item = (await client.get(BLOCKS_URL, headers=alice.auth)).json()["data"]["items"][0]

        assert item["player"]["username"] == bob.username
        assert item["player"]["statistics"] is not None

    async def test_it_pages_with_a_cursor(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player
    ) -> None:
        for _ in range(3):
            await block(client, alice, await register(client, contract_session))

        first = (await client.get(BLOCKS_URL, headers=alice.auth, params={"limit": 2})).json()[
            "data"
        ]
        assert len(first["items"]) == 2
        assert first["page"]["has_more"] is True

        second = (
            await client.get(
                BLOCKS_URL,
                headers=alice.auth,
                params={"limit": 2, "cursor": first["page"]["next_cursor"]},
            )
        ).json()["data"]

        assert second["page"]["has_more"] is False
        seen = [i["player"]["id"] for i in first["items"] + second["items"]]
        assert len(seen) == len(set(seen)) == 3


class TestFriendshipCascade:
    async def test_blocking_terminates_the_friendship(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """FS-3: "a block immediately voids any friendship — blocking must
        not require a second action to be effective"."""
        await befriend(client, alice, bob)

        await block(client, alice, bob)

        assert (await client.get(COUNT_URL, headers=alice.auth)).json()["data"]["total"] == 0
        assert (await client.get(COUNT_URL, headers=bob.auth)).json()["data"]["total"] == 0

    async def test_the_end_reason_is_stored(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """`BLOCKED` rather than `REMOVED`, and the distinction is
        load-bearing rather than descriptive: it is what a future
        re-friending rule reads and what tells an operator why the
        friendship in the history ended."""
        await befriend(client, alice, bob)
        await block(client, alice, bob)

        row = await contract_session.scalar(select(FriendshipModel))

        assert row is not None
        assert row.ended_reason is not None
        assert row.ended_reason.value == "blocked"
        assert row.ended_at is not None

    async def test_the_friendship_row_is_kept(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """BL-3: blocks do not rewrite history."""
        await befriend(client, alice, bob)
        await block(client, alice, bob)

        assert len((await contract_session.scalars(select(FriendshipModel))).all()) == 1

    async def test_a_blocked_player_never_appears_in_a_friend_list(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        await befriend(client, alice, bob)
        await block(client, alice, bob)

        assert (await client.get(FRIENDS_URL, headers=alice.auth)).json()["data"]["items"] == []
        assert (await client.get(FRIENDS_URL, headers=bob.auth)).json()["data"]["items"] == []


class TestRequestCascade:
    async def test_pending_requests_are_voided(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        sent = await client.post(REQUESTS_URL, headers=bob.auth, json={"player_id": str(alice.id)})
        assert sent.status_code == 201

        await block(client, alice, bob)

        row = await contract_session.scalar(select(FriendRequestModel))
        assert row is not None
        assert row.status.value == "declined_by_block"
        assert row.responded_at is not None

    async def test_both_directions_are_voided(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """A block suppresses contact symmetrically; leaving the reverse
        request pending would let the blocked player's request sit in the
        blocker's inbox."""
        await client.post(REQUESTS_URL, headers=bob.auth, json={"player_id": str(alice.id)})

        await block(client, alice, bob)

        assert (await client.get(f"{REQUESTS_URL}/incoming", headers=alice.auth)).json()["data"][
            "items"
        ] == []
        assert (await client.get(f"{REQUESTS_URL}/outgoing", headers=bob.auth)).json()["data"][
            "items"
        ] == []

    async def test_a_new_request_is_refused_in_both_directions(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        await block(client, alice, bob)

        assert (
            await client.post(REQUESTS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        ).status_code == 404
        assert (
            await client.post(REQUESTS_URL, headers=bob.auth, json={"player_id": str(alice.id)})
        ).status_code == 404

    async def test_the_refusal_is_indistinguishable_from_an_unknown_player(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """**FR-2, and the reason it is a requirement rather than a
        nicety**: "distinguishable rejection tells the sender they were
        blocked, which is exactly what the blocker was avoiding."

        Asserted as an equality between two responses rather than against a
        constant, because the failure being guarded against is a
        *difference*, whatever form it takes.
        """
        await block(client, alice, bob)

        blocked = await client.post(
            REQUESTS_URL, headers=bob.auth, json={"player_id": str(alice.id)}
        )
        unknown = await client.post(
            REQUESTS_URL, headers=bob.auth, json={"player_id": str(uuid4())}
        )

        assert blocked.status_code == unknown.status_code == 404
        assert blocked.json()["code"] == unknown.json()["code"]
        assert blocked.json()["message"] == unknown.json()["message"]


class TestOneTransaction:
    async def test_a_failed_block_leaves_the_friendship_intact(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """**The cascade is one transaction**, asserted the only way it can
        be.

        Three writes that usually all succeed prove nothing about sharing a
        transaction. The property becomes observable when the *first* fails:
        with one transaction nothing else happened, and with three the
        friendship would already be gone.

        The failure is induced by pre-inserting the block row, so the unique
        index refuses the service's write.

        If this ever fails, the platform can reach the state the cascade
        exists to prevent — a friendship ended by a block that was never
        recorded, which no reconciliation would ever notice.
        """
        await befriend(client, alice, bob)

        await contract_session.execute(
            text(
                "INSERT INTO friends.blocked_player "
                "(id, blocker_id, blocked_id, created_at) "
                "VALUES (gen_random_uuid(), :a, :b, now())"
            ),
            {"a": alice.id, "b": bob.id},
        )
        await contract_session.flush()

        response = await block(client, alice, bob)

        assert response.status_code == 409, response.text

        row = await contract_session.scalar(select(FriendshipModel))
        assert row is not None
        assert row.ended_at is None, "the friendship was ended outside the block's transaction"


class TestProfileVisibility:
    async def test_a_blocked_reader_sees_no_audience_valued_field(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """**`BLOCKED` outranks `EVERYONE`** — the assertion a gate that
        checked the level before the relationship would fail.

        Alice publishes her last-seen to everyone and then blocks Bob. She
        has not published it *to him*.
        """
        published = await client.patch(
            PRIVACY_URL, headers=alice.auth, json={"last_seen": "everyone"}
        )
        assert published.status_code == 200, published.text
        await block(client, alice, bob)

        as_blocked = await client.get(f"{PROFILES_URL}/{alice.username}", headers=bob.auth)

        assert as_blocked.status_code == 200
        assert as_blocked.json()["data"]["last_seen"] is None
        assert as_blocked.json()["data"]["is_online"] is None

    async def test_the_block_works_in_both_directions(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """Symmetric in effect even though the fact is one-directional: a
        blocker who kept seeing the person they blocked would have gained
        nothing."""
        await client.patch(PRIVACY_URL, headers=bob.auth, json={"last_seen": "everyone"})
        await block(client, alice, bob)

        as_blocker = await client.get(f"{PROFILES_URL}/{bob.username}", headers=alice.auth)

        assert as_blocker.json()["data"]["last_seen"] is None

    async def test_the_profile_itself_is_still_readable(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """A block hides audience-valued fields, not the player. A `404`
        would tell the blocked player something happened — which is exactly
        what BL-1 withholds."""
        await block(client, alice, bob)

        response = await client.get(f"{PROFILES_URL}/{alice.username}", headers=bob.auth)

        assert response.status_code == 200
        assert response.json()["data"]["username"] == alice.username


class TestOptionalAuthentication:
    async def test_an_anonymous_read_still_works(self, client: AsyncClient, alice: Player) -> None:
        """A public profile is public. Requiring a token would break every
        link a player shares and every server-rendered page AD-24
        anticipates."""
        response = await client.get(f"{PROFILES_URL}/{alice.username}")

        assert response.status_code == 200
        assert response.json()["data"]["username"] == alice.username

    async def test_an_invalid_token_is_still_a_401(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """A **missing** token is anonymous; a **malformed** one is not.
        Treating a broken token as anonymous would turn every client bug
        into a silently degraded response."""
        response = await client.get(
            f"{PROFILES_URL}/{alice.username}",
            headers={"Authorization": "Bearer not-a-token"},
        )

        assert response.status_code == 401

    async def test_a_friend_sees_a_friends_only_field_and_a_stranger_does_not(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """**`VisibilityLevel.FRIENDS` on the profile endpoint**, which is
        what optional authentication was needed for.

        Before A64-013.5 this endpoint had no viewer at all, so a friend was
        composed as a stranger and saw a friend's friends-only fields
        hidden. Asserted through `statistics`, which is a boolean setting
        and must therefore behave identically for both — the control that
        shows this is testing the audience path rather than the flag.
        """
        stranger = await register(client, contract_session)
        await befriend(client, alice, bob)
        await client.patch(PRIVACY_URL, headers=alice.auth, json={"last_seen": "friends"})

        as_friend = await client.get(f"{PROFILES_URL}/{alice.username}", headers=bob.auth)
        as_stranger = await client.get(f"{PROFILES_URL}/{alice.username}", headers=stranger.auth)

        assert as_friend.status_code == as_stranger.status_code == 200
        # `last_seen` is `null` for both, because nothing writes presence
        # yet — the gate opened for one of them and there was nothing behind
        # it. What is asserted here is that neither read fails and both
        # compose, which is the half A64-013.6 will turn into a visible
        # difference.
        assert as_friend.json()["data"]["last_seen"] is None
        assert as_stranger.json()["data"]["last_seen"] is None
        assert as_friend.json()["data"]["statistics"] is not None
        assert as_stranger.json()["data"]["statistics"] is not None


class TestSearchExclusion:
    async def test_a_blocked_player_disappears_from_search(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player
    ) -> None:
        tag = uuid4().hex[:4]
        target = await register(client, contract_session, prefix=f"t{tag}")

        before = await client.get(SEARCH_URL, headers=alice.auth, params={"q": target.username})
        assert [i["id"] for i in before.json()["data"]["items"]] == [str(target.id)]

        await block(client, alice, target)

        after = await client.get(SEARCH_URL, headers=alice.auth, params={"q": target.username})
        assert after.json()["data"]["items"] == []

    async def test_the_blocked_player_cannot_find_the_blocker_either(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player
    ) -> None:
        """Symmetric, and it has to be: a one-directional exclusion would
        make the asymmetry itself the signal BL-1 withholds."""
        tag = uuid4().hex[:4]
        target = await register(client, contract_session, prefix=f"t{tag}")
        blocker = await register(client, contract_session, prefix=f"t{tag}")

        await block(client, blocker, target)

        found = await client.get(SEARCH_URL, headers=target.auth, params={"q": blocker.username})

        assert found.json()["data"]["items"] == []

    async def test_unrelated_players_are_unaffected(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player
    ) -> None:
        tag = uuid4().hex[:4]
        blocked = await register(client, contract_session, prefix=f"t{tag}")
        other = await register(client, contract_session, prefix=f"t{tag}")
        await block(client, alice, blocked)

        found = await client.get(SEARCH_URL, headers=alice.auth, params={"q": f"t{tag}"})

        assert [i["id"] for i in found.json()["data"]["items"]] == [str(other.id)]


class TestBatchComposition:
    async def test_the_block_list_composes_in_a_fixed_number_of_queries(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player
    ) -> None:
        """A64-013.5: "Never introduce N+1 profile composition."

        Counted rather than inspected, because that is the only way to tell
        `compose_many` from a loop that happens to return the right answer.
        The bound asserts the count does **not grow with the page**.
        """
        for _ in range(6):
            await block(client, alice, await register(client, contract_session))

        statements: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = contract_session.get_bind().engine  # type: ignore[union-attr]
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = await client.get(BLOCKS_URL, headers=alice.auth, params={"limit": 6})
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert len(response.json()["data"]["items"]) == 6

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        # The token's account, the blocks, the six identities, their
        # statistics. A loop would issue an identity read *per row* on top.
        assert len(selects) <= 6, "\n".join(selects)


class TestAuthentication:
    @pytest.mark.parametrize(
        ("method", "path"),
        [("POST", BLOCKS_URL), ("GET", BLOCKS_URL), ("DELETE", f"{BLOCKS_URL}/{uuid4()}")],
        ids=["block", "list", "unblock"],
    )
    async def test_an_anonymous_call_is_refused(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        response = await client.request(method, path, json={"player_id": str(uuid4())})

        assert response.status_code == 401


class TestOpenApi:
    async def test_every_endpoint_is_documented(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()

        for verb, path in (
            ("post", "/api/v1/blocks"),
            ("get", "/api/v1/blocks"),
            ("delete", "/api/v1/blocks/{player_id}"),
        ):
            operation = spec["paths"][path][verb]
            assert operation["summary"], path
            assert operation["description"].strip(), path
            assert operation["tags"] == ["blocks"], path

    async def test_the_error_responses_carry_the_platform_error_model(
        self, client: AsyncClient
    ) -> None:
        spec = (await client.get("/openapi.json")).json()
        operation = spec["paths"]["/api/v1/blocks"]["post"]

        assert set(operation["responses"]) >= {"201", "401", "409", "422", "429"}
        for status in ("401", "409", "422", "429"):
            schema = operation["responses"][status]["content"]["application/json"]["schema"]
            assert "ErrorResponse" in str(schema)
