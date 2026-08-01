"""The queue endpoints end to end — real PostgreSQL, real constraints, the
real composition root.

A64-014.1's four required queue behaviours — join, duplicate rejected,
leave, expiration — are asserted as *use cases* in
`tests/unit/test_queue_service.py`. This file asserts the parts only HTTP
can show, and deliberately nothing else:

    the wire shapes and status codes
    that the actor is the token and cannot be a parameter
    that the rating is not accepted from a client
    that the OpenAPI document describes what the routes actually do
    that joining wrote a real outbox row in the same transaction

The graph under test is the one that ships. Nothing about `matchmaking` is
overridden — the real repository, the real provisional rating provider and
the real event publisher over the test's session.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.matchmaking.domain.queue_ticket import PROVISIONAL_RATING
from app.platform.outbox import OutboxModel
from tests.contract.contract_app import build_contract_app, contract_client

QUEUE_URL = "/api/v1/matchmaking/queue"
MY_TICKET_URL = f"{QUEUE_URL}/me"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


class Player:
    def __init__(self, player_id: UUID, auth: dict[str, str]) -> None:
        self.id = player_id
        self.auth = auth


async def register(client: AsyncClient) -> Player:
    suffix = uuid4().hex[:8]
    created = await client.post(
        REGISTER_URL,
        json={
            "username": f"player{suffix}",
            "email": f"{suffix}@example.com",
            "password": PASSWORD,
        },
    )
    assert created.status_code == 201, created.text

    signed_in = await client.post(
        LOGIN_URL, json={"email": f"{suffix}@example.com", "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    return Player(
        UUID(created.json()["data"]["id"]),
        {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"},
    )


@pytest_asyncio.fixture
async def alice(client: AsyncClient) -> Player:
    return await register(client)


@pytest_asyncio.fixture
async def bob(client: AsyncClient) -> Player:
    return await register(client)


class TestJoin:
    async def test_joining_returns_the_ticket(self, client: AsyncClient, alice: Player) -> None:
        response = await client.post(
            QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked", "region": "europe"}
        )

        assert response.status_code == 201, response.text
        ticket = response.json()["data"]
        assert ticket["status"] == "waiting"
        assert ticket["queue_type"] == "ranked"
        assert ticket["region"] == "europe"
        assert ticket["waiting"] == 1

    async def test_the_region_defaults_to_global(self, client: AsyncClient, alice: Player) -> None:
        """`global` is not a place — it is the answer for a player who has
        not been located, and it is the default so an unlocated player is
        pairable with everybody rather than with nobody."""
        response = await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "casual"})

        assert response.status_code == 201, response.text
        assert response.json()["data"]["region"] == "global"

    async def test_the_rating_is_the_platform_s_and_not_the_client_s(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """QT-2, and the reason `JoinQueueRequest` forbids extra fields: a
        client-supplied rating would be a self-reported skill level on the
        endpoint that decides who you play."""
        rejected = await client.post(
            QUEUE_URL,
            headers=alice.auth,
            json={"queue_type": "ranked", "rating_snapshot": 3000},
        )
        assert rejected.status_code == 422, rejected.text

        accepted = await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})
        assert accepted.json()["data"]["rating_snapshot"] == PROVISIONAL_RATING

    async def test_an_unknown_queue_type_is_refused(
        self, client: AsyncClient, alice: Player
    ) -> None:
        response = await client.post(
            QUEUE_URL, headers=alice.auth, json={"queue_type": "tournament"}
        )

        assert response.status_code == 422, response.text

    async def test_joining_requires_a_token(self, client: AsyncClient) -> None:
        response = await client.post(QUEUE_URL, json={"queue_type": "ranked"})

        assert response.status_code == 401, response.text

    async def test_joining_writes_an_outbox_row_in_the_same_transaction(
        self, client: AsyncClient, alice: Player, contract_session: AsyncSession
    ) -> None:
        """AD-16. The event is as durable as the ticket, and this is the
        assertion that says so without a worker: the row is visible inside
        the test's own transaction, which it could only be if the publisher
        shared the request's session."""
        await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})

        rows = await contract_session.scalars(
            select(OutboxModel).where(OutboxModel.event_type == "matchmaking.queue_ticket_enqueued")
        )
        events = list(rows)
        assert len(events) == 1
        assert events[0].payload["player_id"] == str(alice.id)
        assert events[0].aggregate_type == "queue_ticket"


class TestVariantSelection:
    """Which game a player may queue for — A64-015.2.

    The engine plays three variants and the platform offers one. The
    distinction is a `ProductVariant` at the boundary rather than a
    validator over the engine's `BoardVariant`, so an unoffered variant is
    refused by the schema and never appears in the document as an accepted
    value.
    """

    async def test_the_russian_pool_is_accepted(self, client: AsyncClient, alice: Player) -> None:
        response = await client.post(
            QUEUE_URL,
            headers=alice.auth,
            json={"variant": "russian_8x8", "queue_type": "ranked"},
        )

        assert response.status_code == 201, response.text
        assert response.json()["data"]["variant"] == "russian_8x8"

    async def test_the_english_pool_is_rejected(self, client: AsyncClient, alice: Player) -> None:
        """A testing and configuration fixture, not a product
        (`specs/game-engine/audit.md` §9). It carries the engine's only
        external perft oracle, so it cannot be deleted — and it must not be
        reachable from an HTTP body."""
        response = await client.post(
            QUEUE_URL,
            headers=alice.auth,
            json={"variant": "english_8x8", "queue_type": "ranked"},
        )

        assert response.status_code == 422, response.text

    async def test_a_rejected_variant_writes_no_ticket(
        self, client: AsyncClient, alice: Player
    ) -> None:
        await client.post(
            QUEUE_URL,
            headers=alice.auth,
            json={"variant": "english_8x8", "queue_type": "ranked"},
        )

        response = await client.get(MY_TICKET_URL, headers=alice.auth)
        assert response.status_code == 404, response.text

    async def test_the_international_pool_is_rejected(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """Its draw rules are a placeholder rather than a claim
        (`specs/game-engine.md` §7.7). Offering it would ship Russian's
        rules under another name."""
        response = await client.post(
            QUEUE_URL,
            headers=alice.auth,
            json={"variant": "international_10x10", "queue_type": "ranked"},
        )

        assert response.status_code == 422, response.text

    async def test_the_variant_defaults_to_the_one_on_offer(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """So a client written against A64-015.1's two-field body keeps
        working, and so the default is a decision rather than an accident
        of whichever member is first."""
        response = await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "casual"})

        assert response.status_code == 201, response.text
        assert response.json()["data"]["variant"] == "russian_8x8"

    async def test_the_ticket_reads_back_with_its_variant(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """It survives the round trip through PostgreSQL's own
        `queue_variant` enum — the column, not just the request."""
        await client.post(
            QUEUE_URL,
            headers=alice.auth,
            json={"variant": "russian_8x8", "queue_type": "casual", "region": "asia"},
        )

        response = await client.get(MY_TICKET_URL, headers=alice.auth)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["variant"] == "russian_8x8"
        assert response.json()["data"]["region"] == "asia"

    async def test_a_response_names_no_variant_the_platform_does_not_offer(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """The refusal lists what *is* offered — a client that guessed
        wrong needs that — and names nothing else. An error message
        assembled from `BoardVariant` would advertise the fixture in the
        act of rejecting it."""
        response = await client.post(
            QUEUE_URL,
            headers=alice.auth,
            json={"variant": "english_8x8", "queue_type": "ranked"},
        )

        body = response.text
        assert "russian_8x8" in body
        assert "english_8x8" not in body
        assert "international_10x10" not in body


class TestDuplicateJoin:
    async def test_a_second_join_conflicts(self, client: AsyncClient, alice: Player) -> None:
        await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})

        response = await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "conflict"

    async def test_a_second_join_in_another_pool_conflicts(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """QT-1 is across all pools. This is the assertion an index keyed on
        `(player_id, queue_type)` would fail and every other one here would
        pass."""
        await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})

        response = await client.post(
            QUEUE_URL, headers=alice.auth, json={"queue_type": "casual", "region": "asia"}
        )

        assert response.status_code == 409, response.text

    async def test_another_player_may_join_the_same_pool(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})

        response = await client.post(QUEUE_URL, headers=bob.auth, json={"queue_type": "ranked"})

        assert response.status_code == 201, response.text
        assert response.json()["data"]["waiting"] == 2


class TestLeave:
    async def test_leaving_returns_no_content(self, client: AsyncClient, alice: Player) -> None:
        await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})

        response = await client.delete(QUEUE_URL, headers=alice.auth)

        assert response.status_code == 204, response.text

    async def test_leaving_when_not_queued_is_idempotent(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """One answer for both cases, so the status code never reports queue
        state back to a probe — and a client retrying a dropped response is
        not told the resource is gone by its own first attempt."""
        assert (await client.delete(QUEUE_URL, headers=alice.auth)).status_code == 204
        assert (await client.delete(QUEUE_URL, headers=alice.auth)).status_code == 204

    async def test_a_player_may_re_queue_after_leaving(
        self, client: AsyncClient, alice: Player
    ) -> None:
        await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})
        await client.delete(QUEUE_URL, headers=alice.auth)

        response = await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "casual"})

        assert response.status_code == 201, response.text

    async def test_leaving_requires_a_token(self, client: AsyncClient) -> None:
        assert (await client.delete(QUEUE_URL)).status_code == 401


class TestReadMyTicket:
    async def test_a_live_ticket_is_returned(self, client: AsyncClient, alice: Player) -> None:
        joined = await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})

        response = await client.get(MY_TICKET_URL, headers=alice.auth)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["ticket_id"] == joined.json()["data"]["ticket_id"]

    async def test_no_ticket_is_a_404(self, client: AsyncClient, alice: Player) -> None:
        response = await client.get(MY_TICKET_URL, headers=alice.auth)

        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"

    async def test_a_left_queue_reads_as_a_404(self, client: AsyncClient, alice: Player) -> None:
        """Indistinguishable from "never joined" — which of the two applies
        is not something this endpoint should answer."""
        await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})
        await client.delete(QUEUE_URL, headers=alice.auth)

        assert (await client.get(MY_TICKET_URL, headers=alice.auth)).status_code == 404

    async def test_one_player_cannot_read_another_s_ticket(
        self, client: AsyncClient, alice: Player, bob: Player
    ) -> None:
        """There is no parameter that could name another player, so this
        asserts the *absence* of a surface rather than a rejection: bob's
        token on the same URL reports bob's queue state, which is none."""
        await client.post(QUEUE_URL, headers=alice.auth, json={"queue_type": "ranked"})

        assert (await client.get(MY_TICKET_URL, headers=bob.auth)).status_code == 404

    async def test_reading_requires_a_token(self, client: AsyncClient) -> None:
        assert (await client.get(MY_TICKET_URL)).status_code == 401


class TestOpenApi:
    """The generated document, asserted rather than trusted.

    An endpoint whose handler can 409 but whose decorator does not mention
    409 is documented wrongly, and nothing else catches it — the code works,
    the schema is valid, and only a client integrator finds out
    (`app/api/openapi.py`).
    """

    async def test_all_three_endpoints_are_documented(self, contract_session: AsyncSession) -> None:
        schema = build_contract_app(contract_session).openapi()

        assert "post" in schema["paths"][QUEUE_URL]
        assert "delete" in schema["paths"][QUEUE_URL]
        assert "get" in schema["paths"][MY_TICKET_URL]

    async def test_every_documented_failure_carries_the_platform_error_shape(
        self, contract_session: AsyncSession
    ) -> None:
        """`error_response` binds `ErrorResponse` so a route cannot document
        a failure without it — otherwise FastAPI promises `{"detail": ...}`,
        a shape no endpoint on this platform has ever returned."""
        schema = build_contract_app(contract_session).openapi()
        join = schema["paths"][QUEUE_URL]["post"]["responses"]

        assert set(join) >= {"201", "401", "409", "422", "429"}
        for status in ("401", "409", "422", "429"):
            content = join[status]["content"]["application/json"]["schema"]
            assert content["$ref"].endswith("/ErrorResponse")

    async def test_the_read_documents_its_404(self, contract_session: AsyncSession) -> None:
        schema = build_contract_app(contract_session).openapi()

        assert "404" in schema["paths"][MY_TICKET_URL]["get"]["responses"]

    async def test_the_matchmaking_tag_is_described(self, contract_session: AsyncSession) -> None:
        """A tag with no entry in `OPENAPI_TAGS` renders as a bare name, and
        a reader has to infer from the endpoint list what the group is."""
        schema = build_contract_app(contract_session).openapi()

        described = {tag["name"] for tag in schema["tags"] if tag.get("description")}
        assert "matchmaking" in described

    async def test_the_document_advertises_only_offered_variants(
        self, contract_session: AsyncSession
    ) -> None:
        """A generated client should be unable to *express* a request for a
        variant the platform does not run. A validator over the engine's
        wider enum would have produced the opposite: the fixture documented
        as an accepted value and refused at runtime."""
        schema = build_contract_app(contract_session).openapi()
        variants = schema["components"]["schemas"]["ProductVariant"]["enum"]

        assert variants == ["russian_8x8"]

    async def test_neither_request_nor_response_names_the_engine_s_enum(
        self, contract_session: AsyncSession
    ) -> None:
        """`BoardVariant` is `engine`'s, and `matchmaking` does not import
        it (R-2). If it ever reached the document, the boundary that keeps
        the fixture off the menu would already be gone."""
        schema = build_contract_app(contract_session).openapi()

        assert "BoardVariant" not in schema["components"]["schemas"]

    async def test_the_join_body_forbids_unknown_fields(
        self, contract_session: AsyncSession
    ) -> None:
        """`extra="forbid"` reaches the schema as `additionalProperties:
        false`, which is what makes "you cannot send a rating" visible to a
        generated client rather than only to a 422."""
        schema = build_contract_app(contract_session).openapi()
        body = schema["components"]["schemas"]["JoinQueueRequest"]

        assert body["additionalProperties"] is False
