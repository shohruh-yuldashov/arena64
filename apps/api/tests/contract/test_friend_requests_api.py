"""The friend-request API end to end — real PostgreSQL, real constraints,
the real composition root.

A64-013.2 names twelve required cases across send, accept, decline, cancel,
performance and rate limiting. All of them are here.

What else is here is the small set of properties that would be *silently*
wrong rather than loudly broken, and that no unit test can reach:

  - **the partial unique index is what enforces FR-1**, asserted by driving
    a real violation rather than by trusting the validator that checks it
    first;
  - **a page is composed in one batch**, asserted by counting queries — the
    only way to tell `compose_many` from a loop that happens to work;
  - **cancelling does not delete**, asserted against the row rather than
    against the response, because "history must remain available" is a
    claim about storage;
  - **the two list endpoints show the other party**, which is the kind of
    thing that is trivially inverted and passes every test that only counts
    rows.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_factory import create_app
from app.modules.avatars.presentation.rate_limits import enforce_avatar_upload_limit
from app.modules.friends.infrastructure.models import FriendRequestModel
from app.modules.friends.presentation.rate_limits import (
    enforce_friend_request_respond_limit,
    enforce_friend_request_send_limit,
)
from app.modules.profiles.presentation.rate_limits import PROFILE_READ_RATE_LIMIT
from tests.contract.contract_app import build_contract_app, contract_client
from tests.unit.test_auth_rate_limits import api_routes

REQUESTS_URL = "/api/v1/friends/requests"
INCOMING_URL = f"{REQUESTS_URL}/incoming"
OUTGOING_URL = f"{REQUESTS_URL}/outgoing"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """A plain application, for the structural assertions only.

    Module-scoped and deliberately *not* the `client` fixture's app: nothing
    below it sends a request, so it needs no session, no overrides and no
    database — it is the route table under inspection.
    """
    return create_app()


def _route(app: FastAPI, method: str, path: str) -> APIRoute:
    """One route by method and fully-prefixed path.

    Reuses `tests/unit/test_auth_rate_limits.api_routes`, which walks nested
    routers — FastAPI keeps an included router as one opaque entry rather
    than flattening it, so the obvious `app.routes` loop finds nothing and
    every assertion built on it passes vacuously. That helper has a test of
    its own guarding exactly that.
    """
    for route_path, route in api_routes(app):
        if route_path == path and method in (route.methods or set()):
            return route
    raise AssertionError(f"no route for {method} {path}")


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app over the test's rolled-back transaction.

    No override on any friends service, validator, repository or schema —
    the graph under test is the one that ships, including the real partial
    unique index. Only `lifespan`'s state is stood in for
    (`tests/contract/contract_app.py`).
    """
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


class Player:
    """A registered, signed-in account: its id and its bearer header."""

    def __init__(self, player_id: UUID, auth: dict[str, str]) -> None:
        self.id = player_id
        self.auth = auth


async def register(client: AsyncClient) -> Player:
    suffix = uuid4().hex[:10]
    account = {
        "username": f"player{suffix}",
        "email": f"{suffix}@example.com",
        "password": PASSWORD,
    }
    created = await client.post(REGISTER_URL, json=account)
    assert created.status_code == 201, created.text
    player_id = UUID(created.json()["data"]["id"])

    signed_in = await client.post(LOGIN_URL, json={"email": account["email"], "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    return Player(
        player_id, {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"}
    )


@pytest_asyncio.fixture
async def alice(client: AsyncClient) -> Player:
    return await register(client)


@pytest_asyncio.fixture
async def bob(client: AsyncClient) -> Player:
    return await register(client)


async def send(client: AsyncClient, sender: Player, recipient: Player) -> Any:
    return await client.post(
        REQUESTS_URL, headers=sender.auth, json={"player_id": str(recipient.id)}
    )


async def send_ok(client: AsyncClient, sender: Player, recipient: Player) -> dict[str, Any]:
    response = await send(client, sender, recipient)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()["data"]
    return body


class TestSend:
    async def test_a_request_is_created(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        body = await send_ok(client, alice, bob)

        assert body["status"] == "pending"
        assert body["responded_at"] is None
        # The *recipient*, composed exactly as a profile page would be.
        assert body["player"]["id"] == str(bob.id)

    async def test_a_duplicate_request_is_refused(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """FR-1: at most one pending request per ordered pair. "Otherwise
        'send request' becomes a harassment primitive"."""
        await send_ok(client, alice, bob)

        response = await send(client, alice, bob)

        assert response.status_code == 409
        assert response.json()["code"] == "duplicate_friend_request"

    async def test_the_opposite_direction_is_refused(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """Refused rather than auto-accepted, and with its own code.

        Two people each sending a request is not the same event as one
        accepting the other's — nobody has agreed to anything. The distinct
        code is what lets a client offer the right next step: accept the
        request you already have.
        """
        await send_ok(client, alice, bob)

        response = await send(client, bob, alice)

        assert response.status_code == 409
        assert response.json()["code"] == "opposite_friend_request_pending"

    async def test_a_self_request_is_refused(self, client: AsyncClient, alice: Player) -> None:
        response = await send(client, alice, alice)

        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    async def test_the_uniqueness_rule_is_enforced_by_the_index_not_the_validator(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """BE-06, asserted rather than assumed.

        The validator checks FR-1 first to produce a good error cheaply, and
        two concurrent sends both pass it — only the partial unique index is
        correct under concurrency. This drives a second row straight at the
        database, bypassing the validator entirely, and asserts the
        constraint refuses it.
        """
        await send_ok(client, alice, bob)

        with pytest.raises(Exception, match="uq_friend_request__one_pending_per_pair"):
            await contract_session.execute(
                text(
                    "INSERT INTO friends.friend_request "
                    "(id, requester_id, addressee_id, status, created_at, version) "
                    "VALUES (gen_random_uuid(), :a, :b, 'pending', now(), 0)"
                ),
                {"a": alice.id, "b": bob.id},
            )

    async def test_a_new_request_is_allowed_after_the_previous_one_resolved(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """Why the index is **partial**. A plain unique on the pair would
        permit only one request ever between two players, so a friendship
        that ended could never be re-requested."""
        first = await send_ok(client, alice, bob)
        cancelled = await client.delete(f"{REQUESTS_URL}/{first['id']}", headers=alice.auth)
        assert cancelled.status_code == 200, cancelled.text

        assert (await send(client, alice, bob)).status_code == 201


class TestAccept:
    async def test_the_recipient_can_accept(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        request = await send_ok(client, alice, bob)

        response = await client.post(f"{REQUESTS_URL}/{request['id']}/accept", headers=bob.auth)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "accepted"
        assert response.json()["data"]["responded_at"] is not None

    async def test_the_sender_cannot_accept(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """The ownership rule that matters most: a sender who could accept
        would be adding themselves to somebody else's friend list."""
        request = await send_ok(client, alice, bob)

        response = await client.post(f"{REQUESTS_URL}/{request['id']}/accept", headers=alice.auth)

        assert response.status_code == 403

    async def test_a_third_party_cannot_accept(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        stranger = await register(client)
        request = await send_ok(client, alice, bob)

        response = await client.post(
            f"{REQUESTS_URL}/{request['id']}/accept", headers=stranger.auth
        )

        assert response.status_code == 403

    async def test_accepting_twice_is_a_conflict(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """The two-device race, in sequence. The version column is what
        makes it hold when the two are genuinely concurrent."""
        request = await send_ok(client, alice, bob)
        await client.post(f"{REQUESTS_URL}/{request['id']}/accept", headers=bob.auth)

        response = await client.post(f"{REQUESTS_URL}/{request['id']}/accept", headers=bob.auth)

        assert response.status_code == 409

    async def test_an_accepted_request_is_kept(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """A64-013.2: "Do NOT delete accepted rows. History must remain
        available." Asserted against the row, because that is a claim about
        storage rather than about a response."""
        request = await send_ok(client, alice, bob)
        await client.post(f"{REQUESTS_URL}/{request['id']}/accept", headers=bob.auth)

        stored = await contract_session.scalar(
            select(FriendRequestModel).where(FriendRequestModel.id == UUID(request["id"]))
        )

        assert stored is not None
        assert stored.status.value == "accepted"


class TestDecline:
    async def test_the_recipient_can_decline(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        request = await send_ok(client, alice, bob)

        response = await client.post(f"{REQUESTS_URL}/{request['id']}/decline", headers=bob.auth)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "declined"

    async def test_the_sender_cannot_decline(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        request = await send_ok(client, alice, bob)

        response = await client.post(f"{REQUESTS_URL}/{request['id']}/decline", headers=alice.auth)

        assert response.status_code == 403

    async def test_a_declined_request_leaves_the_senders_outgoing_list_silently(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """FR-3. The row survives — FR-5's future cooldown reads it — but the
        sender is told nothing and simply stops seeing it."""
        request = await send_ok(client, alice, bob)
        await client.post(f"{REQUESTS_URL}/{request['id']}/decline", headers=bob.auth)

        outgoing = await client.get(OUTGOING_URL, headers=alice.auth)

        assert outgoing.json()["data"]["items"] == []


class TestCancel:
    async def test_the_sender_can_cancel(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        request = await send_ok(client, alice, bob)

        response = await client.delete(f"{REQUESTS_URL}/{request['id']}", headers=alice.auth)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "cancelled"

    async def test_the_recipient_cannot_cancel(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """They have `decline`, which reaches the same practical outcome and
        leaves different history."""
        request = await send_ok(client, alice, bob)

        response = await client.delete(f"{REQUESTS_URL}/{request['id']}", headers=bob.auth)

        assert response.status_code == 403

    async def test_cancelling_does_not_delete_the_row(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        """`DELETE` on the wire, `UPDATE` in storage. Nothing here is ever
        removed — a request that ended is a fact with a date."""
        request = await send_ok(client, alice, bob)
        await client.delete(f"{REQUESTS_URL}/{request['id']}", headers=alice.auth)

        stored = await contract_session.scalar(
            select(FriendRequestModel).where(FriendRequestModel.id == UUID(request["id"]))
        )

        assert stored is not None
        assert stored.status.value == "cancelled"
        assert stored.responded_at is not None


class TestLists:
    async def test_incoming_shows_the_sender(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """Trivially invertible, and it would pass any test that only counted
        rows."""
        await send_ok(client, alice, bob)

        items = (await client.get(INCOMING_URL, headers=bob.auth)).json()["data"]["items"]

        assert [item["player"]["id"] for item in items] == [str(alice.id)]

    async def test_outgoing_shows_the_recipient(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        await send_ok(client, alice, bob)

        items = (await client.get(OUTGOING_URL, headers=alice.auth)).json()["data"]["items"]

        assert [item["player"]["id"] for item in items] == [str(bob.id)]

    async def test_a_request_appears_in_neither_list_for_a_third_party(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        stranger = await register(client)
        await send_ok(client, alice, bob)

        assert (await client.get(INCOMING_URL, headers=stranger.auth)).json()["data"]["items"] == []
        assert (await client.get(OUTGOING_URL, headers=stranger.auth)).json()["data"]["items"] == []

    async def test_the_item_carries_no_party_identifiers(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """A64-013.2: "never expose unnecessary user identifiers." The list
        already says which direction it is, and `player.id` is what a client
        acts on."""
        await send_ok(client, alice, bob)

        item = (await client.get(INCOMING_URL, headers=bob.auth)).json()["data"]["items"][0]

        assert set(item) == {"id", "status", "player", "created_at", "responded_at"}
        assert "requester_id" not in item
        assert "version" not in item

    async def test_pages_with_a_cursor_and_never_repeats(
        self, client: AsyncClient, bob: Player
    ) -> None:
        senders = [await register(client) for _ in range(5)]
        for sender in senders:
            await send_ok(client, sender, bob)

        first = (await client.get(INCOMING_URL, headers=bob.auth, params={"limit": 2})).json()[
            "data"
        ]
        assert len(first["items"]) == 2
        assert first["page"]["has_more"] is True

        second = (
            await client.get(
                INCOMING_URL,
                headers=bob.auth,
                params={"limit": 2, "cursor": first["page"]["next_cursor"]},
            )
        ).json()["data"]
        third = (
            await client.get(
                INCOMING_URL,
                headers=bob.auth,
                params={"limit": 2, "cursor": second["page"]["next_cursor"]},
            )
        ).json()["data"]

        assert third["page"]["has_more"] is False
        seen = [i["id"] for i in first["items"] + second["items"] + third["items"]]
        assert len(seen) == len(set(seen)) == 5

    async def test_a_malformed_cursor_is_422(self, client: AsyncClient, bob: Player) -> None:
        response = await client.get(INCOMING_URL, headers=bob.auth, params={"cursor": "nope"})

        assert response.status_code == 422


class TestBatchComposition:
    async def test_a_page_composes_in_a_fixed_number_of_queries(
        self, client: AsyncClient, contract_session: AsyncSession, bob: Player
    ) -> None:
        """A64-013.2: "Never perform N+1 profile composition."

        Counted rather than inspected, because that is the only way to tell
        `compose_many` from a loop that happens to return the right answer.
        The bound is deliberately generous — this asserts that the count
        does **not grow with the page**, which is the property that matters,
        rather than pinning an exact number that any unrelated query change
        would break.
        """
        senders = [await register(client) for _ in range(6)]
        for sender in senders:
            await send_ok(client, sender, bob)

        statements: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = contract_session.get_bind().engine  # type: ignore[union-attr]
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = await client.get(INCOMING_URL, headers=bob.auth, params={"limit": 6})
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert len(response.json()["data"]["items"]) == 6

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        # One for the token's account, one for the requests, one for the six
        # players' identities, one for their statistics. A loop would issue
        # at least one identity read *per row* on top of that.
        assert len(selects) <= 6, "\n".join(selects)


class TestRateLimiting:
    """A64-013.2's rate-limiting requirements, asserted **structurally**.

    Against the route table rather than by sending enough requests to be
    refused. Sending them would couple these tests to the configured limit
    and to a shared Redis counter with an hour-long window — the coupling
    `tests/conftest.py` disables limiting suite-wide to avoid. What the
    brief asks for is that the guards are *applied*, and that is a property
    of the wiring which no amount of tuning can break.

    `tests/contract/test_rate_limiting_api.py` covers the mechanism itself
    against real Redis, and `tests/unit/test_auth_rate_limits.py` owns the
    route walker these reuse.
    """

    def test_every_friend_request_write_carries_a_user_scoped_guard(self, app: FastAPI) -> None:
        """The four endpoints A64-013.2 names, each with its guard.

        Asserted per endpoint rather than as a count, so a guard attached to
        the wrong route fails here rather than passing on a total.
        """
        expected = {
            ("POST", "/api/v1/friends/requests"): enforce_friend_request_send_limit,
            ("POST", "/api/v1/friends/requests/{request_id}/accept"): (
                enforce_friend_request_respond_limit
            ),
            ("POST", "/api/v1/friends/requests/{request_id}/decline"): (
                enforce_friend_request_respond_limit
            ),
            ("DELETE", "/api/v1/friends/requests/{request_id}"): (
                enforce_friend_request_respond_limit
            ),
        }

        for (method, path), guard in expected.items():
            route = _route(app, method, path)
            assert guard in [d.call for d in route.dependant.dependencies], f"{method} {path}"

    def test_the_migrated_endpoints_are_guarded_with_the_right_dimension(
        self, app: FastAPI
    ) -> None:
        """A64-013.2 also asks that `GET /profiles/{username}` and
        `POST /profile/avatar` be migrated to "the correct rate limiting".

        The two dimensions differ and both are correct: the profile read is
        anonymous, so per **IP** is the only dimension available; the avatar
        upload is authenticated, so per **user** is the better one. Both
        were unlimited before this task, and the previous three each
        recorded that as debt.
        """
        profile_read = _route(app, "GET", "/api/v1/profiles/{username}")
        assert PROFILE_READ_RATE_LIMIT in [d.call for d in profile_read.dependant.dependencies]

        avatar_upload = _route(app, "POST", "/api/v1/profile/avatar")
        assert enforce_avatar_upload_limit in [d.call for d in avatar_upload.dependant.dependencies]


class TestAuthentication:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", REQUESTS_URL),
            ("GET", INCOMING_URL),
            ("GET", OUTGOING_URL),
        ],
        ids=["send", "incoming", "outgoing"],
    )
    async def test_an_anonymous_call_is_refused(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        response = await client.request(method, path, json={"player_id": str(uuid4())})

        assert response.status_code == 401


class TestOpenApi:
    async def test_every_endpoint_is_documented(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()

        documented = {
            ("post", "/api/v1/friends/requests"),
            ("get", "/api/v1/friends/requests/incoming"),
            ("get", "/api/v1/friends/requests/outgoing"),
            ("post", "/api/v1/friends/requests/{request_id}/accept"),
            ("post", "/api/v1/friends/requests/{request_id}/decline"),
            ("delete", "/api/v1/friends/requests/{request_id}"),
        }

        for verb, path in documented:
            operation = spec["paths"][path][verb]
            assert operation["summary"], path
            assert operation["description"].strip(), path
            assert operation["tags"] == ["friends"], path

    async def test_the_error_responses_carry_the_platform_error_model(
        self, client: AsyncClient
    ) -> None:
        spec = (await client.get("/openapi.json")).json()
        operation = spec["paths"]["/api/v1/friends/requests"]["post"]

        assert set(operation["responses"]) >= {"201", "401", "409", "422", "429"}
        for status in ("401", "409", "422", "429"):
            schema = operation["responses"][status]["content"]["application/json"]["schema"]
            assert "ErrorResponse" in str(schema)
