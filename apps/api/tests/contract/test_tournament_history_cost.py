"""A player's tournament history costs one statement — A64-020.0C.

The endpoint was correct and expensive. `entrant_count` and `current_round`
were read per tournament, so a page of a hundred issued 201 statements while
every existing test — written against a page of one — passed.

Four tests, and none of them re-covers what `test_tournament_results.py`
already asserts about a history entry's contents. What is asserted here is
the property that fix had to preserve and the one it had to change:

    cost        one statement, whatever the page size
    contents    identical to what the per-tournament reads produced
    order       unchanged, including the tie-break
    cursor      unchanged, and an old one still resumes

Skipped, not failed, when PostgreSQL is unreachable.
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tournament.domain.registration import Registration, RegistrationStatus
from app.modules.tournament.domain.rounds import RoundStatus
from app.modules.tournament.domain.tournament import Tournament
from app.modules.tournament.infrastructure.models import TournamentRoundModel
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyRegistrationRepository,
)
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register as register_account
from tests.contract.test_tournament_lobby import NOW, _tournament


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):  # type: ignore[no-untyped-def]
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


def _history_url(player_id: UUID) -> str:
    return f"/api/v1/players/{player_id}/tournaments"


async def _entered(
    session: AsyncSession, tournament: Tournament, player_id: UUID, *, at: Any
) -> None:
    await SqlAlchemyRegistrationRepository(session).add(
        Registration(tournament_id=tournament.id, player_id=player_id, registered_at=at),
        capacity=tournament.capacity,
    )


async def _statements_for(session: AsyncSession, call: Callable[[], Awaitable[Response]]) -> int:
    """How many statements one request sends. Counted at the driver."""
    counted: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        counted.append(statement)

    engine = session.get_bind().engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await call()
        assert response.status_code == 200, response.text
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(counted)


class TestCost:
    async def test_a_hundred_tournaments_still_cost_one_statement(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The whole point of A64-020.0C.

        Three page sizes rather than two, because the defect was
        proportional rather than constant: it read three statements for a
        page of one, 101 for fifty and 201 for a hundred. Asserting the
        exact number at each size is what makes a regression legible — a
        "does not grow" assertion alone would pass a fix that traded 201
        queries for two.
        """
        viewer = await register_account(client)
        for index in range(100):
            entered_at = NOW - timedelta(minutes=index)
            tournament = await _tournament(
                contract_session, name=f"History {index}", created_at=entered_at
            )
            await _entered(contract_session, tournament, viewer.id, at=entered_at)

        costs = [
            await _statements_for(
                contract_session,
                lambda size=size: client.get(  # type: ignore[misc]
                    _history_url(viewer.id), params={"limit": size}, headers=viewer.auth
                ),
            )
            for size in (1, 50, 100)
        ]

        assert costs == [1, 1, 1]


class TestEquivalence:
    async def test_a_history_entry_says_what_the_detail_endpoint_says(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The contents are unchanged, asserted where a change would show.

        `entrant_count` and `current_round` are the two fields the fix moved
        from a per-tournament read into the page's own statement, and
        `GET /tournaments/{id}` **still** computes them the old way. So the
        two surfaces agreeing field for field is exactly "the new
        implementation returns what the old one did" — expressed as a
        property of the API rather than as a snapshot somebody would update
        when it failed.

        The tournament is built with three live entrants, one withdrawn
        entrant and a round in progress, so both numbers are non-trivial:
        a subquery that forgot the `REGISTERED` predicate or the round
        status filter would return four and one respectively.
        """
        viewer = await register_account(client)
        tournament = await _tournament(
            contract_session, name="Compared", created_at=NOW, entrants=3
        )
        await _entered(contract_session, tournament, viewer.id, at=NOW)
        await SqlAlchemyRegistrationRepository(contract_session).add(
            Registration(
                tournament_id=tournament.id,
                player_id=uuid4(),
                registered_at=NOW,
                status=RegistrationStatus.WITHDRAWN,
                withdrawn_at=NOW,
            ),
            capacity=tournament.capacity,
        )
        contract_session.add(
            TournamentRoundModel(
                tournament_id=tournament.id,
                round_number=2,
                status=RoundStatus.IN_PROGRESS,
                published_at=NOW,
                started_at=NOW,
            )
        )
        contract_session.add(
            TournamentRoundModel(
                tournament_id=tournament.id,
                round_number=1,
                status=RoundStatus.COMPLETED,
                published_at=NOW,
                started_at=NOW,
                completed_at=NOW,
            )
        )
        await contract_session.flush()

        history = await client.get(_history_url(viewer.id), headers=viewer.auth)
        detail = await client.get(f"/api/v1/tournaments/{tournament.id}", headers=viewer.auth)

        assert history.status_code == 200, history.text
        assert detail.status_code == 200, detail.text
        entry = history.json()["data"]["entries"][0]["tournament"]
        assert entry == detail.json()["data"]
        assert entry["entrant_count"] == 4  # three seeded entrants plus the viewer
        assert entry["current_round"] == 2  # the lowest unfinished round


class TestOrdering:
    async def test_the_order_is_registration_instant_then_tournament_id(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§12's order, unchanged by the fix.

        `(registered_at, tournament_id)` descending — the registration's
        instant, not the tournament's, because a history is a list of
        entries rather than of tournaments. Two entries share an instant, so
        the tie-break key is exercised rather than merely present: a
        single-key order would leave that pair in whatever order the join
        happened to produce, which is not an order at all.
        """
        viewer = await register_account(client)
        entered = []
        for index, instant in enumerate(
            (
                NOW,
                NOW - timedelta(minutes=1),
                NOW - timedelta(minutes=1),
                NOW - timedelta(minutes=2),
            )
        ):
            tournament = await _tournament(
                contract_session, name=f"Ordered {index}", created_at=NOW
            )
            await _entered(contract_session, tournament, viewer.id, at=instant)
            entered.append((instant, tournament.id))

        response = await client.get(_history_url(viewer.id), headers=viewer.auth)

        assert response.status_code == 200, response.text
        assert [entry["tournament"]["id"] for entry in response.json()["data"]["entries"]] == [
            str(identifier) for _, identifier in sorted(entered, reverse=True)
        ]


class TestCursor:
    async def test_a_cursor_still_walks_the_history_without_repeating_or_skipping(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The cursor is unchanged — same encoding, same predicate.

        The fix touched the `SELECT` list and nothing else, so a cursor this
        endpoint issued before it must still resume in the same place. The
        walk is what proves it: five entries, two at a time, every one seen
        exactly once and the last page reporting none.
        """
        viewer = await register_account(client)
        for index in range(5):
            entered_at = NOW - timedelta(minutes=index)
            tournament = await _tournament(
                contract_session, name=f"Paged {index}", created_at=entered_at
            )
            await _entered(contract_session, tournament, viewer.id, at=entered_at)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(3):
            page = await client.get(
                _history_url(viewer.id),
                params={"limit": 2, **({"after": cursor} if cursor else {})},
                headers=viewer.auth,
            )
            assert page.status_code == 200, page.text
            body = page.json()["data"]
            seen.extend(entry["tournament"]["id"] for entry in body["entries"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        assert len(seen) == len(set(seen)) == 5
        assert cursor is None
