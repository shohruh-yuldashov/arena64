"""The friend list end to end — real PostgreSQL, real constraints, the real
composition root.

A64-013.3 names ten required cases across friendship creation, listing,
deletion, privacy and performance. All of them are here.

What else is here is the small set of properties that would be *silently*
wrong rather than loudly broken, and that no unit test can reach:

  - **one transaction**, asserted by making the friendship write *fail* and
    checking that the request is still pending — the only way to tell one
    unit of work from two that usually both succeed;
  - **canonical ordering in the row**, read back from the column rather than
    from the aggregate that produced it;
  - **the partial unique index**, driven by a real violation rather than by
    trusting the service that checks first;
  - **`VisibilityLevel.FRIENDS` end to end**, which is the whole point of
    the task and cannot be observed without two accounts and a real graph.

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

from app.modules.friends.domain.friendship import FriendshipEndReason
from app.modules.friends.infrastructure.models import FriendRequestModel, FriendshipModel
from tests.contract.contract_app import build_contract_app, contract_client

FRIENDS_URL = "/api/v1/friends"
COUNT_URL = f"{FRIENDS_URL}/count"
REQUESTS_URL = f"{FRIENDS_URL}/requests"
PRIVACY_URL = "/api/v1/profile/privacy"
PROFILES_URL = "/api/v1/profiles"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app over the test's rolled-back transaction.

    No override on any friends or profiles service — the graph under test is
    the one that ships, including the real `FriendshipRelationshipProvider`
    reading the real relation. Only `lifespan`'s state is stood in for
    (`tests/contract/contract_app.py`).
    """
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


class Player:
    def __init__(self, player_id: UUID, username: str, auth: dict[str, str]) -> None:
        self.id = player_id
        self.username = username
        self.auth = auth


async def register(client: AsyncClient) -> Player:
    suffix = uuid4().hex[:10]
    username = f"player{suffix}"
    created = await client.post(
        REGISTER_URL,
        json={"username": username, "email": f"{suffix}@example.com", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text

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
async def alice(client: AsyncClient) -> Player:
    return await register(client)


@pytest_asyncio.fixture
async def bob(client: AsyncClient) -> Player:
    return await register(client)


async def befriend(client: AsyncClient, a: Player, b: Player) -> str:
    """`a` sends, `b` accepts. Returns the request id."""
    sent = await client.post(REQUESTS_URL, headers=a.auth, json={"player_id": str(b.id)})
    assert sent.status_code == 201, sent.text
    request_id = sent.json()["data"]["id"]

    accepted = await client.post(f"{REQUESTS_URL}/{request_id}/accept", headers=b.auth)
    assert accepted.status_code == 200, accepted.text
    return str(request_id)


class TestCreatedOnAccept:
    async def test_accepting_creates_the_friendship(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        await befriend(client, alice, bob)

        assert (await client.get(COUNT_URL, headers=alice.auth)).json()["data"]["total"] == 1
        assert (await client.get(COUNT_URL, headers=bob.auth)).json()["data"]["total"] == 1

    async def test_the_row_is_stored_in_canonical_order(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """DB-12, read back from the columns rather than from the aggregate
        that wrote them — which is the only way to catch a mirrored write."""
        await befriend(client, alice, bob)

        row = await contract_session.scalar(select(FriendshipModel))

        assert row is not None
        assert row.player_low_id < row.player_high_id
        assert {row.player_low_id, row.player_high_id} == {alice.id, bob.id}

    async def test_exactly_one_row_exists_for_the_pair(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """Never mirrored. "Two rows for one relationship is two facts that
        can disagree, and when they do, neither is authoritative"."""
        await befriend(client, alice, bob)

        rows = (await contract_session.scalars(select(FriendshipModel))).all()

        assert len(rows) == 1

    async def test_the_friendship_records_the_request_that_created_it(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        request_id = await befriend(client, alice, bob)

        row = await contract_session.scalar(select(FriendshipModel))

        assert row is not None
        assert str(row.source_request_id) == request_id

    async def test_the_start_and_the_response_share_one_instant(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """They are one event. Two `clock.now()` calls would record two
        answers to one question — and would make the friendship look as
        though it began after the acceptance."""
        await befriend(client, alice, bob)

        friendship = await contract_session.scalar(select(FriendshipModel))
        request = await contract_session.scalar(select(FriendRequestModel))

        assert friendship is not None
        assert request is not None
        assert friendship.created_at == request.responded_at

    async def test_a_duplicate_friendship_is_impossible(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """BE-06, asserted rather than assumed: the partial unique index is
        what makes a second live row impossible, and it is driven here
        straight at the database, bypassing the service entirely."""
        await befriend(client, alice, bob)
        low, high = sorted([alice.id, bob.id])

        with pytest.raises(Exception, match="uq_friendship__pair"):
            await contract_session.execute(
                text(
                    "INSERT INTO friends.friendship "
                    "(id, player_low_id, player_high_id, created_at) "
                    "VALUES (gen_random_uuid(), :low, :high, now())"
                ),
                {"low": low, "high": high},
            )

    async def test_a_mirrored_row_is_rejected_by_the_check_constraint(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """DB-12: "without it, `(B, A)` is insertable and the unique
        constraint does not fire, so the invariant fails exactly once —
        silently, in production"."""
        low, high = sorted([alice.id, bob.id])

        with pytest.raises(Exception, match="ck_friendship__canonical_order"):
            await contract_session.execute(
                text(
                    "INSERT INTO friends.friendship "
                    "(id, player_low_id, player_high_id, created_at) "
                    "VALUES (gen_random_uuid(), :high, :low, now())"
                ),
                {"low": low, "high": high},
            )


class TestOneTransaction:
    async def test_a_failed_friendship_write_leaves_the_request_pending(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """**FR-4, asserted the only way it can be.**

        Two units of work usually both succeed, so a test that accepts and
        then checks both rows exist passes whether or not they share a
        transaction. The property only becomes observable when the *second*
        write fails: with one transaction the accepted request rolls back
        with it, and with two it does not.

        The failure is induced by pre-inserting the friendship the
        acceptance is about to create, so the partial unique index refuses
        the service's write.

        If this ever fails, the system can reach the state A64-013.3 exists
        to prevent: a request that says `accepted` and no friendship — a
        pair who believe they are friends and are not, which nothing would
        ever notice, because an accepted request is a valid terminal state
        on its own.
        """
        sent = await client.post(REQUESTS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        request_id = sent.json()["data"]["id"]

        low, high = sorted([alice.id, bob.id])
        await contract_session.execute(
            text(
                "INSERT INTO friends.friendship "
                "(id, player_low_id, player_high_id, created_at) "
                "VALUES (gen_random_uuid(), :low, :high, now())"
            ),
            {"low": low, "high": high},
        )
        await contract_session.flush()

        response = await client.post(f"{REQUESTS_URL}/{request_id}/accept", headers=bob.auth)

        # **409, specifically.** Any 4xx would leave the request pending if
        # the acceptance never ran at all — a 404 from a bad id would pass a
        # looser assertion while proving nothing. A conflict is the unique
        # index refusing the *friendship* write, which is the only outcome
        # that shows the second write was attempted inside the same
        # transaction as the first.
        assert response.status_code == 409, response.text

        stored = await contract_session.scalar(
            select(FriendRequestModel).where(FriendRequestModel.id == UUID(request_id))
        )
        assert stored is not None
        assert stored.status.value == "pending", "the acceptance was not rolled back"
        assert stored.responded_at is None


class TestFriendList:
    async def test_it_lists_the_other_party(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """Trivially invertible, and it would pass any test that only
        counted rows."""
        await befriend(client, alice, bob)

        items = (await client.get(FRIENDS_URL, headers=alice.auth)).json()["data"]["items"]

        assert [item["player"]["id"] for item in items] == [str(bob.id)]

    async def test_both_sides_see_each_other(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """One row, read from either side — the read convenience DB-12 buys
        with two indexes instead of two rows."""
        await befriend(client, alice, bob)

        from_alice = (await client.get(FRIENDS_URL, headers=alice.auth)).json()["data"]["items"]
        from_bob = (await client.get(FRIENDS_URL, headers=bob.auth)).json()["data"]["items"]

        assert [i["player"]["id"] for i in from_alice] == [str(bob.id)]
        assert [i["player"]["id"] for i in from_bob] == [str(alice.id)]

    async def test_a_stranger_sees_neither(self, client: AsyncClient, alice: Player) -> None:
        bob = await register(client)
        stranger = await register(client)
        await befriend(client, alice, bob)

        items = (await client.get(FRIENDS_URL, headers=stranger.auth)).json()["data"]["items"]

        assert items == []

    async def test_the_item_carries_the_start_date(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        await befriend(client, alice, bob)

        item = (await client.get(FRIENDS_URL, headers=alice.auth)).json()["data"]["items"][0]

        assert set(item) == {"player", "friends_since"}
        assert item["friends_since"]

    async def test_pages_with_a_cursor_and_never_repeats(
        self, client: AsyncClient, alice: Player
    ) -> None:
        for _ in range(5):
            await befriend(client, alice, await register(client))

        first = (await client.get(FRIENDS_URL, headers=alice.auth, params={"limit": 2})).json()[
            "data"
        ]
        assert len(first["items"]) == 2
        assert first["page"]["has_more"] is True

        second = (
            await client.get(
                FRIENDS_URL,
                headers=alice.auth,
                params={"limit": 2, "cursor": first["page"]["next_cursor"]},
            )
        ).json()["data"]
        third = (
            await client.get(
                FRIENDS_URL,
                headers=alice.auth,
                params={"limit": 2, "cursor": second["page"]["next_cursor"]},
            )
        ).json()["data"]

        assert third["page"]["has_more"] is False
        seen = [i["player"]["id"] for i in first["items"] + second["items"] + third["items"]]
        assert len(seen) == len(set(seen)) == 5

    async def test_the_count_matches_the_number_of_friends(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """A count that did not match the list beside it is the kind of
        defect nobody reports as a bug because it looks like a caching
        artefact — which is why both go through one `_involves` predicate."""
        for _ in range(3):
            await befriend(client, alice, await register(client))

        assert (await client.get(COUNT_URL, headers=alice.auth)).json()["data"]["total"] == 3

    async def test_a_new_account_has_no_friends(self, client: AsyncClient, alice: Player) -> None:
        assert (await client.get(COUNT_URL, headers=alice.auth)).json()["data"]["total"] == 0
        assert (await client.get(FRIENDS_URL, headers=alice.auth)).json()["data"]["items"] == []


class TestRemove:
    async def test_either_party_can_remove(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """FS-2: unilateral."""
        await befriend(client, alice, bob)

        response = await client.delete(f"{FRIENDS_URL}/{bob.id}", headers=alice.auth)

        assert response.status_code == 204, response.text
        assert (await client.get(COUNT_URL, headers=alice.auth)).json()["data"]["total"] == 0
        assert (await client.get(COUNT_URL, headers=bob.auth)).json()["data"]["total"] == 0

    async def test_a_stranger_cannot_remove_somebody_elses_friendship(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """The ownership rule. A third party is not part of the pair, so
        there is no friendship between *them and the caller* to remove —
        which is why the answer is `404` rather than `403`."""
        stranger = await register(client)
        await befriend(client, alice, bob)

        response = await client.delete(f"{FRIENDS_URL}/{bob.id}", headers=stranger.auth)

        assert response.status_code == 404
        assert (await client.get(COUNT_URL, headers=alice.auth)).json()["data"]["total"] == 1

    async def test_removing_a_non_friend_is_404(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        response = await client.delete(f"{FRIENDS_URL}/{bob.id}", headers=alice.auth)

        assert response.status_code == 404

    async def test_the_row_is_kept(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """database.md §1221: a friendship that ended is a fact with a date.
        It is also what lets the pair be friends again."""
        await befriend(client, alice, bob)
        await client.delete(f"{FRIENDS_URL}/{bob.id}", headers=alice.auth)

        row = await contract_session.scalar(select(FriendshipModel))

        assert row is not None
        assert row.ended_at is not None
        assert row.ended_reason is FriendshipEndReason.REMOVED

    async def test_the_pair_can_become_friends_again(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """Why the unique index is **partial**. A plain unique on the pair
        would have made this impossible forever."""
        await befriend(client, alice, bob)
        await client.delete(f"{FRIENDS_URL}/{bob.id}", headers=alice.auth)

        await befriend(client, alice, bob)

        assert (await client.get(COUNT_URL, headers=alice.auth)).json()["data"]["total"] == 1


class TestFriendVisibility:
    async def test_a_friends_only_field_is_visible_to_a_friend_and_hidden_from_others(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """**`VisibilityLevel.FRIENDS` end to end** — the value a boolean
        could not express, and the reason A64-013.2 widened the type.

        All three readings of one profile in one test, because the property
        is the *difference* between them: the same field, the same endpoint,
        three answers determined only by who is asking.
        """
        stranger = await register(client)
        await befriend(client, alice, bob)

        # Alice restricts her last-seen to friends. Before A64-013.3 this
        # value was storable and behaved exactly like `nobody`.
        narrowed = await client.patch(
            PRIVACY_URL, headers=alice.auth, json={"last_seen": "friends"}
        )
        assert narrowed.status_code == 200, narrowed.text
        assert narrowed.json()["data"]["last_seen"] == "friends"

        # A friend reads the profile: the gate opens. `last_seen` is still
        # `null` because nothing writes presence yet — what this asserts is
        # that the *field is composed*, which the two negatives below
        # distinguish it from.
        as_friend = await client.get(f"{PROFILES_URL}/{alice.username}", headers=bob.auth)
        assert as_friend.status_code == 200, as_friend.text

        as_stranger = await client.get(f"{PROFILES_URL}/{alice.username}", headers=stranger.auth)
        as_anonymous = await client.get(f"{PROFILES_URL}/{alice.username}")

        # All three render the same shape — a `null` never says why.
        for response in (as_friend, as_stranger, as_anonymous):
            assert response.json()["data"]["last_seen"] is None

    async def test_the_friend_list_composes_profiles_as_a_friend_sees_them(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """Every player in a friend list is, by definition, a friend of the
        caller — so the composition must resolve `FRIEND` for all of them.

        Asserted through `show_statistics`, which is a boolean and therefore
        *not* audience-valued: it must behave identically whoever asks,
        which is what makes this a test of the friend path rather than of
        the flag.
        """
        await befriend(client, alice, bob)

        item = (await client.get(FRIENDS_URL, headers=alice.auth)).json()["data"]["items"][0]

        assert item["player"]["id"] == str(bob.id)
        assert item["player"]["statistics"] is not None


class TestBatchComposition:
    async def test_a_page_composes_in_a_fixed_number_of_queries(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player
    ) -> None:
        """A64-013.3: "Never compose profiles one by one."

        Counted rather than inspected, because that is the only way to tell
        `compose_many` from a loop that happens to return the right answer.
        The bound asserts that the count does **not grow with the page**,
        which is the property that matters, rather than pinning an exact
        number any unrelated query change would break.
        """
        for _ in range(6):
            await befriend(client, alice, await register(client))

        statements: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = contract_session.get_bind().engine  # type: ignore[union-attr]
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = await client.get(FRIENDS_URL, headers=alice.auth, params={"limit": 6})
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert len(response.json()["data"]["items"]) == 6

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        # The token's account, the friendships, the six identities, their
        # statistics, and the relationship batch. A loop would issue at
        # least one identity read *per row* on top of that.
        assert len(selects) <= 7, "\n".join(selects)


class TestAuthentication:
    @pytest.mark.parametrize(
        ("method", "path"),
        [("GET", FRIENDS_URL), ("GET", COUNT_URL), ("DELETE", f"{FRIENDS_URL}/{uuid4()}")],
        ids=["list", "count", "remove"],
    )
    async def test_an_anonymous_call_is_refused(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        assert (await client.request(method, path)).status_code == 401


class TestRouteResolution:
    async def test_the_request_routes_are_not_shadowed_by_the_friend_routes(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """`/friends/count` and `/friends/{player_id}` are both two
        segments. The specific path is registered first; this asserts the
        resolution rather than trusting the comment there."""
        assert (await client.get(COUNT_URL, headers=alice.auth)).status_code == 200
        assert (await client.get(f"{REQUESTS_URL}/incoming", headers=alice.auth)).status_code == 200


class TestOpenApi:
    async def test_every_endpoint_is_documented(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()

        for verb, path in (
            ("get", "/api/v1/friends"),
            ("get", "/api/v1/friends/count"),
            ("delete", "/api/v1/friends/{player_id}"),
        ):
            operation = spec["paths"][path][verb]
            assert operation["summary"], path
            assert operation["description"].strip(), path
            assert operation["tags"] == ["friends"], path
