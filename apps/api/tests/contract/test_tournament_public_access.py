"""Tournaments, read without an account — A64-026.4 §43.

Through the real v1 router and real PostgreSQL, because what is being
asserted is an **authorization boundary** and a boundary asserted against a
mock is a boundary asserted against the mock.

Four things have to hold at once, and each is a way this change could be
wrong:

    an anonymous caller reads a published tournament
    an anonymous caller cannot see a draft, and cannot tell it exists
    an authenticated caller still sees everything, drafts included
    every mutation still requires an account

The fourth is the one worth writing down. Opening a read is a small edit and
opening a write is the same edit made once too often; nothing here would
notice a `CurrentUser` disappearing from `enter_tournament` except a test
that looks.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.domain.tournament import (
    Tournament,
    TournamentFormat,
    TournamentStatus,
)
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyTournamentRepository,
)
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register as register_account

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
LOBBY_URL = "/api/v1/tournaments"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):  # type: ignore[no-untyped-def]
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def _tournament(session: AsyncSession, *, name: str, status: TournamentStatus) -> Tournament:
    return await SqlAlchemyTournamentRepository(session).create(
        Tournament(
            id=uuid4(),
            name=name,
            format=TournamentFormat.SINGLE_ELIMINATION,
            variant=ProductVariant.RUSSIAN_8X8,
            speed_class=SpeedClass.CLASSICAL,
            rated=True,
            capacity=8,
            created_by=uuid4(),
            created_at=NOW,
            registration_deadline=NOW + timedelta(hours=1),
            status=status,
        )
    )


class TestAnonymousReads:
    async def test_the_lobby_answers_without_a_token(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The change this task exists for.

        A64-026.1's landing page describes tournaments and, until now, could
        not show one: `/tournaments` bounced a visitor to sign-in, so the
        link would have been a link that lies about where it goes.
        """
        await _tournament(
            contract_session, name="Open Cup", status=TournamentStatus.REGISTRATION_OPEN
        )

        response = await client.get(LOBBY_URL)

        assert response.status_code == 200, response.text
        names = [entry["name"] for entry in response.json()["data"]["entries"]]
        assert "Open Cup" in names

    async def test_a_completed_tournament_is_visible_to_a_visitor(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """`is_published` is deliberately not the inverse of `is_terminal`.

        A finished bracket is the most useful thing here to show somebody
        without an account — it is a record of something that happened, and
        hiding it would answer "what happens here?" with silence.
        """
        await _tournament(contract_session, name="Autumn Final", status=TournamentStatus.COMPLETED)

        response = await client.get(LOBBY_URL)

        assert response.status_code == 200, response.text
        names = [entry["name"] for entry in response.json()["data"]["entries"]]
        assert "Autumn Final" in names

    async def test_the_detail_and_bracket_answer_without_a_token(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        tournament = await _tournament(
            contract_session, name="Open Cup", status=TournamentStatus.IN_PROGRESS
        )

        detail = await client.get(f"{LOBBY_URL}/{tournament.id}")
        bracket = await client.get(f"{LOBBY_URL}/{tournament.id}/bracket")
        standings = await client.get(f"{LOBBY_URL}/{tournament.id}/standings")

        assert detail.status_code == 200, detail.text
        assert bracket.status_code == 200, bracket.text
        assert standings.status_code == 200, standings.text


class TestDraftsStayHidden:
    async def test_a_draft_is_absent_from_an_anonymous_lobby(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """`DRAFT` is the one state the enum itself calls "not yet
        advertised" — a tournament whose operator has not decided it exists.
        """
        await _tournament(contract_session, name="Unannounced", status=TournamentStatus.DRAFT)
        await _tournament(
            contract_session, name="Open Cup", status=TournamentStatus.REGISTRATION_OPEN
        )

        response = await client.get(LOBBY_URL)

        assert response.status_code == 200, response.text
        names = [entry["name"] for entry in response.json()["data"]["entries"]]
        assert "Open Cup" in names
        assert "Unannounced" not in names

    async def test_a_draft_answers_404_rather_than_403(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Not-found, because forbidden is an oracle.

        A `403` confirms the id names something, which is the only fact an
        enumerating caller wants from an endpoint keyed by UUID. `404` is
        the same answer an id naming nothing gets, which is what makes
        guessing worthless.
        """
        draft = await _tournament(
            contract_session, name="Unannounced", status=TournamentStatus.DRAFT
        )
        absent = uuid4()

        hidden = await client.get(f"{LOBBY_URL}/{draft.id}")
        missing = await client.get(f"{LOBBY_URL}/{absent}")

        assert hidden.status_code == 404
        assert missing.status_code == 404

        # Indistinguishable but for the two per-request identifiers, which
        # differ by construction and carry nothing about the tournament.
        # Everything else a caller could compare across the two requests is
        # identical — a differing code or message would rebuild the oracle
        # the status code removes.
        def comparable(body: dict[str, object]) -> dict[str, object]:
            per_request = {"request_id", "correlation_id"}
            return {key: value for key, value in body.items() if key not in per_request}

        assert comparable(hidden.json()) == comparable(missing.json())

    async def test_a_draft_bracket_and_standings_hide_too(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The guard is on all three reads, not only the one somebody
        remembered. A bracket that answered for a hidden tournament would
        leak the entrant list the detail refused."""
        draft = await _tournament(
            contract_session, name="Unannounced", status=TournamentStatus.DRAFT
        )

        bracket = await client.get(f"{LOBBY_URL}/{draft.id}/bracket")
        standings = await client.get(f"{LOBBY_URL}/{draft.id}/standings")

        assert bracket.status_code == 404
        assert standings.status_code == 404


class TestAuthenticatedIsUnchanged:
    async def test_a_signed_in_player_still_sees_drafts(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The narrowing applies to the anonymous path only.

        The lobby a player has seen since A64-020.0B is unchanged — this
        task opened a door, it did not close one.
        """
        await _tournament(contract_session, name="Unannounced", status=TournamentStatus.DRAFT)
        viewer = await register_account(client, contract_session)

        response = await client.get(LOBBY_URL, headers=viewer.auth)

        assert response.status_code == 200, response.text
        names = [entry["name"] for entry in response.json()["data"]["entries"]]
        assert "Unannounced" in names

    async def test_a_signed_in_player_can_open_a_draft(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        draft = await _tournament(
            contract_session, name="Unannounced", status=TournamentStatus.DRAFT
        )
        viewer = await register_account(client, contract_session)

        response = await client.get(f"{LOBBY_URL}/{draft.id}", headers=viewer.auth)

        assert response.status_code == 200, response.text


class TestMutationsStillRequireAnAccount:
    async def test_entering_and_withdrawing_reject_an_anonymous_caller(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The assertion that makes opening the reads safe to repeat.

        Nothing else in this suite would notice a `VerifiedUser` quietly
        becoming optional on a write, and that is exactly the edit somebody
        makes while opening the next read.
        """
        tournament = await _tournament(
            contract_session, name="Open Cup", status=TournamentStatus.REGISTRATION_OPEN
        )

        entered = await client.post(f"{LOBBY_URL}/{tournament.id}/registrations")
        withdrew = await client.delete(f"{LOBBY_URL}/{tournament.id}/registrations/me")

        assert entered.status_code == 401, entered.text
        assert withdrew.status_code == 401, withdrew.text

    async def test_reading_your_own_entry_still_requires_an_account(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """`my_registration` is the one read here that is genuinely about a
        viewer, so it keeps its token — there is no anonymous answer to
        "am I in this tournament"."""
        tournament = await _tournament(
            contract_session, name="Open Cup", status=TournamentStatus.REGISTRATION_OPEN
        )

        response = await client.get(f"{LOBBY_URL}/{tournament.id}/registrations/me")

        assert response.status_code == 401, response.text


class TestNothingOperationalIsPublished:
    async def test_an_anonymous_entry_carries_the_same_fields_as_before(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """`TournamentSummary` was already the public read model.

        `created_by` is operational and has never been published, and this
        change did not widen the response — an anonymous caller sees no
        field an authenticated one does not, and no field it should not.
        """
        await _tournament(
            contract_session, name="Open Cup", status=TournamentStatus.REGISTRATION_OPEN
        )

        anonymous = await client.get(LOBBY_URL)
        viewer = await register_account(client, contract_session)
        authenticated = await client.get(LOBBY_URL, headers=viewer.auth)

        anonymous_entry = anonymous.json()["data"]["entries"][0]
        authenticated_entry = next(
            entry
            for entry in authenticated.json()["data"]["entries"]
            if entry["id"] == anonymous_entry["id"]
        )
        assert set(anonymous_entry) == set(authenticated_entry)
        assert "created_by" not in anonymous_entry
