"""The acceptance endpoints end to end — real PostgreSQL, real constraints,
the real composition root (A64-015.4 §7).

`tests/unit/test_match_acceptance.py` asserts the lifecycle as a use case.
This file asserts the parts only HTTP can show, and deliberately nothing
else:

    the wire shapes and status codes
    that the actor is the token and cannot be a parameter
    that a match somebody else is in answers exactly like one that does
    not exist
    that no pairing internal reaches the response
    that the opponent preview is one lookup rather than a per-item read

The graph under test is the one that ships. Nothing about `matchmaking` or
`game` is overridden — the real repository, the real acceptance service, the
real `users` profile reader and the real event publisher over the test's
session.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION
from app.modules.game.domain.match_record import MatchRecord, MatchSeat
from app.modules.game.infrastructure import SqlAlchemyMatchRecordRepository
from app.modules.game.public import ProductVariant
from tests.contract.contract_app import build_contract_app, contract_client

PENDING_URL = "/api/v1/matchmaking/matches/pending"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"

WINDOW = timedelta(seconds=30)


def _accept_url(match_id: UUID) -> str:
    return f"/api/v1/matchmaking/matches/{match_id}/accept"


def _decline_url(match_id: UUID) -> str:
    return f"/api/v1/matchmaking/matches/{match_id}/decline"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


class Player:
    def __init__(self, player_id: UUID, username: str, auth: dict[str, str]) -> None:
        self.id = player_id
        self.username = username
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
        f"player{suffix}",
        {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"},
    )


@pytest_asyncio.fixture
async def alice(client: AsyncClient) -> Player:
    return await register(client)


@pytest_asyncio.fixture
async def bob(client: AsyncClient) -> Player:
    return await register(client)


@pytest_asyncio.fixture
async def carol(client: AsyncClient) -> Player:
    return await register(client)


async def _pair(
    session: AsyncSession,
    light: Player,
    dark: Player,
    *,
    created_ago: timedelta = timedelta(0),
) -> MatchRecord:
    """A pending match between two registered players.

    Written through the real repository rather than by driving a pairing
    scan: what this file is about is the three endpoints, and a scan needs a
    pool deep enough to pair — which would make every test here depend on
    the rating window as well.
    """
    at = datetime.now(UTC) - created_ago
    record = MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(player_id=light.id, queue_ticket_id=generate_uuid7()),
        dark=MatchSeat(player_id=dark.id, queue_ticket_id=generate_uuid7()),
        created_at=at,
        acceptance_deadline=at + WINDOW,
    )
    stored, _ = await SqlAlchemyMatchRecordRepository(session).create(record)
    await session.commit()
    return stored


class TestReadingYourPendingMatch:
    async def test_a_player_with_no_pending_match_gets_404(
        self, client: AsyncClient, alice: Player
    ) -> None:
        """Covering "never paired", "already answered" and "expired"
        indistinguishably — which of the three applies is not something this
        endpoint should answer, and the client's next move is the same."""
        response = await client.get(PENDING_URL, headers=alice.auth)

        assert response.status_code == 404, response.text

    async def test_a_participant_reads_the_offer(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        record = await _pair(contract_session, alice, bob)

        response = await client.get(PENDING_URL, headers=alice.auth)

        assert response.status_code == 200, response.text
        match = response.json()["data"]
        assert match["match_id"] == str(record.id)
        assert match["status"] == "pending_acceptance"
        assert match["your_side"] == "light"
        assert match["rated"] is True
        assert match["you_accepted"] is False
        assert match["opponent_accepted"] is False

    async def test_each_side_reads_it_from_their_own_seat(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        """Named from the reader's seat, so a client cannot render the wrong
        half by picking the wrong field."""
        await _pair(contract_session, alice, bob)

        light = (await client.get(PENDING_URL, headers=alice.auth)).json()["data"]
        dark = (await client.get(PENDING_URL, headers=bob.auth)).json()["data"]

        assert light["your_side"] == "light"
        assert dark["your_side"] == "dark"
        assert light["opponent"]["username"] == bob.username
        assert dark["opponent"]["username"] == alice.username

    async def test_the_opponent_preview_carries_only_public_identity(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        """`UserSummary`'s three fields. No email, no join date, no
        country — the endpoint is a match card rather than a second,
        ungated profile renderer."""
        await _pair(contract_session, alice, bob)

        opponent = (await client.get(PENDING_URL, headers=alice.auth)).json()["data"]["opponent"]

        assert set(opponent) == {"player_id", "username", "display_name"}
        assert opponent["player_id"] == str(bob.id)

    async def test_no_pairing_internal_reaches_the_wire(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        """§7's list, asserted against the response rather than against the
        schema: a `pairing_id` or a queue ticket id here would let a client
        correlate two players' matches into a picture of what the scan is
        considering."""
        await _pair(contract_session, alice, bob)

        match = (await client.get(PENDING_URL, headers=alice.auth)).json()["data"]

        for withheld in (
            "pairing_id",
            "light_ticket_id",
            "dark_ticket_id",
            "queue_ticket_id",
            "reserved_until",
            "settled_at",
        ):
            assert withheld not in match

    async def test_the_read_needs_a_token(self, client: AsyncClient) -> None:
        assert (await client.get(PENDING_URL)).status_code == 401


class TestAccepting:
    async def test_one_acceptance_leaves_the_match_pending(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        record = await _pair(contract_session, alice, bob)

        response = await client.post(_accept_url(record.id), headers=alice.auth)

        assert response.status_code == 200, response.text
        match = response.json()["data"]
        assert match["status"] == "pending_acceptance"
        assert match["you_accepted"] is True

    async def test_both_acceptances_activate_the_match(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        record = await _pair(contract_session, alice, bob)

        await client.post(_accept_url(record.id), headers=alice.auth)
        response = await client.post(_accept_url(record.id), headers=bob.auth)

        assert response.json()["data"]["status"] == "active"
        assert response.json()["data"]["opponent_accepted"] is True

    async def test_accepting_twice_is_idempotent(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        """A client retrying after a dropped response asked for something
        already true; a `409` would make a network blip look like a lost
        game."""
        record = await _pair(contract_session, alice, bob)

        first = await client.post(_accept_url(record.id), headers=alice.auth)
        second = await client.post(_accept_url(record.id), headers=alice.auth)

        assert second.status_code == 200
        assert second.json()["data"] == first.json()["data"]

    async def test_the_first_acceptor_learns_the_match_activated(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        """A64-020.5A, and this test previously asserted the opposite.

        It required `404` once both players accepted — which contradicted
        this endpoint's own published schema ("a client that polls after
        answering sees the outcome rather than a `404`") and made the flow
        unfinishable for one of the two players. Acceptance is bilateral,
        so the match activates on the **second** request: Alice's own
        response says `pending_acceptance`, and polling was her only way to
        find out it had started.

        The requirement was outdated, not the code. What replaced it is the
        sentence the schema already promised.
        """
        record = await _pair(contract_session, alice, bob)
        await client.post(_accept_url(record.id), headers=alice.auth)
        await client.post(_accept_url(record.id), headers=bob.auth)

        polled = await client.get(PENDING_URL, headers=alice.auth)

        assert polled.status_code == 200, polled.text
        assert polled.json()["data"]["match_id"] == str(record.id)
        assert polled.json()["data"]["status"] == "active"
        assert polled.json()["data"]["you_accepted"] is True

    async def test_a_declined_match_stops_being_current(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        """The bound on the widening, and the reason it needs no horizon: a
        match leaves this read by settling into something that is not a
        game. Without this, "current" would quietly mean "ever"."""
        record = await _pair(contract_session, alice, bob)
        await client.post(_decline_url(record.id), headers=bob.auth)

        assert (await client.get(PENDING_URL, headers=alice.auth)).status_code == 404

    async def test_accepting_needs_a_token(
        self, client: AsyncClient, contract_session: AsyncSession, alice: Player, bob: Player
    ) -> None:
        record = await _pair(contract_session, alice, bob)

        assert (await client.post(_accept_url(record.id))).status_code == 401


class TestDeclining:
    async def test_a_decline_cancels_the_match(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        record = await _pair(contract_session, alice, bob)

        response = await client.post(_decline_url(record.id), headers=bob.auth)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "cancelled"

    async def test_the_opponent_cannot_activate_a_declined_match(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        record = await _pair(contract_session, alice, bob)
        await client.post(_decline_url(record.id), headers=bob.auth)

        response = await client.post(_accept_url(record.id), headers=alice.auth)

        assert response.status_code == 409, response.text

    async def test_a_decline_after_the_opponent_accepted_still_cancels(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        record = await _pair(contract_session, alice, bob)
        await client.post(_accept_url(record.id), headers=alice.auth)

        response = await client.post(_decline_url(record.id), headers=bob.auth)

        assert response.json()["data"]["status"] == "cancelled"

    async def test_neither_player_is_returned_to_the_queue(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        """§10's stated policy, asserted so it cannot change by accident: a
        declined match leaves both players out of the queue, and they rejoin
        by hand.

        Deliberate and provisional — `specs/matchmaking.md` lists what a
        declined acceptance should cost each side as an open question. The
        day it is answered, this test changes with the behaviour rather than
        the behaviour changing quietly.
        """
        record = await _pair(contract_session, alice, bob)
        await client.post(_accept_url(record.id), headers=alice.auth)
        await client.post(_decline_url(record.id), headers=bob.auth)

        assert (
            await client.get("/api/v1/matchmaking/queue/me", headers=alice.auth)
        ).status_code == 404
        assert (
            await client.get("/api/v1/matchmaking/queue/me", headers=bob.auth)
        ).status_code == 404


class TestOnlyAParticipantMayAnswer:
    async def test_a_stranger_gets_the_same_404_as_an_unknown_match(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
        carol: Player,
    ) -> None:
        """The whole security property of these two routes: a distinct
        status for somebody else's match would make live match identifiers
        enumerable."""
        record = await _pair(contract_session, alice, bob)

        theirs = await client.post(_accept_url(record.id), headers=carol.auth)
        nobodys = await client.post(_accept_url(generate_uuid7()), headers=carol.auth)

        assert theirs.status_code == 404
        assert nobodys.status_code == 404
        assert theirs.json()["code"] == nobodys.json()["code"] == "not_found"

    async def test_a_stranger_cannot_decline_somebody_else_s_match(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
        carol: Player,
    ) -> None:
        record = await _pair(contract_session, alice, bob)

        assert (await client.post(_decline_url(record.id), headers=carol.auth)).status_code == 404

    async def test_a_stranger_changes_nothing(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
        carol: Player,
    ) -> None:
        record = await _pair(contract_session, alice, bob)
        await client.post(_decline_url(record.id), headers=carol.auth)

        still_pending = await client.get(PENDING_URL, headers=alice.auth)

        assert still_pending.json()["data"]["status"] == "pending_acceptance"

    async def test_there_is_no_way_to_name_the_answering_player(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
        carol: Player,
    ) -> None:
        """No path segment, query or body field names who is answering, so
        acting as somebody else is not something the API can express. A body
        that tried is ignored rather than honoured."""
        record = await _pair(contract_session, alice, bob)

        response = await client.post(
            _accept_url(record.id), headers=carol.auth, json={"player_id": str(alice.id)}
        )

        assert response.status_code == 404
        assert (await client.get(PENDING_URL, headers=alice.auth)).json()["data"][
            "you_accepted"
        ] is False


class TestTheAcceptanceDeadline:
    async def test_an_answer_after_the_deadline_is_refused(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        """Refused by the *instant*, not by the reconciler having run: the
        deadline is the rule and the background job is only the
        bookkeeping."""
        record = await _pair(
            contract_session, alice, bob, created_ago=WINDOW + timedelta(seconds=5)
        )

        response = await client.post(_accept_url(record.id), headers=alice.auth)

        assert response.status_code == 409, response.text

    async def test_a_late_decline_is_refused_too(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        record = await _pair(
            contract_session, alice, bob, created_ago=WINDOW + timedelta(seconds=5)
        )

        assert (await client.post(_decline_url(record.id), headers=bob.auth)).status_code == 409

    async def test_the_response_carries_the_deadline_as_an_instant(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        alice: Player,
        bob: Player,
    ) -> None:
        """An instant rather than a countdown, so a slow response cannot
        make a client's timer wrong."""
        record = await _pair(contract_session, alice, bob)

        match = (await client.get(PENDING_URL, headers=alice.auth)).json()["data"]

        assert datetime.fromisoformat(match["acceptance_deadline"]) == record.acceptance_deadline


class TestTheDocument:
    async def test_the_three_routes_are_published(self, contract_session: AsyncSession) -> None:
        app = build_contract_app(contract_session)
        paths = app.openapi()["paths"]

        assert "/api/v1/matchmaking/matches/pending" in paths
        assert "/api/v1/matchmaking/matches/{match_id}/accept" in paths
        assert "/api/v1/matchmaking/matches/{match_id}/decline" in paths

    async def test_the_two_writes_are_rate_limited_and_the_read_is_not(
        self, contract_session: AsyncSession
    ) -> None:
        """§7 requires USER-scoped limiting on the writes.
        `GET .../pending` carries none, for the reason
        `GET /matchmaking/queue/me` carries none: it is the endpoint a
        client polls while deciding."""
        app = build_contract_app(contract_session)
        paths = app.openapi()["paths"]

        assert "429" in paths["/api/v1/matchmaking/matches/{match_id}/accept"]["post"]["responses"]
        assert "429" in paths["/api/v1/matchmaking/matches/{match_id}/decline"]["post"]["responses"]
        assert "429" not in paths["/api/v1/matchmaking/matches/pending"]["get"]["responses"]
