"""The tournament lobby over HTTP — A64-020.0B.

Six tests, through the real v1 router and real PostgreSQL. The lobby is the
first screen of the Tournament UI and the only tournament read a client
reaches without already knowing an id, so what is asserted here is what a
frontend depends on: a deterministic order, a cursor it can round-trip, a
filter that narrows, and a page whose cost does not grow with its size.

Deliberately **not** re-tested: a tournament's detail, its bracket and its
standings. Those are `test_tournament_results.py`'s, and duplicating them
would grow this suite without covering anything new.

The fixtures write tournament rows directly rather than driving the
lifecycle services. That is the point of a read test — a completed and a
cancelled tournament are two rows this endpoint must include, and playing
eight brackets to obtain them would test the write path again and make the
ordering assertions depend on wall-clock ordering.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.domain.registration import Registration
from app.modules.tournament.domain.tournament import (
    Tournament,
    TournamentFormat,
    TournamentStatus,
)
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyRegistrationRepository,
    SqlAlchemyTournamentRepository,
)
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register as register_account

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
LOBBY_URL = "/api/v1/tournaments"

#: Every field the lobby publishes. Asserted as an exact set, so a column
#: added to the summary cannot reach a client without somebody deciding it
#: is public — `created_by`, the no-show deadlines and the attendance rows
#: are the ones this guards.
PUBLIC_FIELDS = {
    "id",
    "name",
    "format",
    "variant",
    "speed_class",
    "rated",
    "capacity",
    "status",
    "entrant_count",
    "current_round",
    "registration_deadline",
    "created_at",
    "started_at",
    "completed_at",
}


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):  # type: ignore[no-untyped-def]
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def _tournament(
    session: AsyncSession,
    *,
    name: str,
    created_at: datetime,
    status: TournamentStatus = TournamentStatus.REGISTRATION_OPEN,
    variant: ProductVariant = ProductVariant.RUSSIAN_8X8,
    speed_class: SpeedClass = SpeedClass.CLASSICAL,
    rated: bool = True,
    capacity: int = 8,
    entrants: int = 0,
) -> Tournament:
    """One tournament in a chosen state, with `entrants` live entries."""
    tournament = await SqlAlchemyTournamentRepository(session).create(
        Tournament(
            id=uuid4(),
            name=name,
            format=TournamentFormat.SINGLE_ELIMINATION,
            variant=variant,
            speed_class=speed_class,
            rated=rated,
            capacity=capacity,
            created_by=uuid4(),
            created_at=created_at,
            registration_deadline=created_at + timedelta(hours=1),
            status=status,
        )
    )
    registrations = SqlAlchemyRegistrationRepository(session)
    for _ in range(entrants):
        await registrations.add(
            Registration(tournament_id=tournament.id, player_id=uuid4(), registered_at=created_at),
            capacity=capacity,
        )
    return tournament


async def _statements_for(session: AsyncSession, call: Callable[[], Awaitable[Response]]) -> int:
    """How many statements one request sends. Counted at the driver.

    The same instrument `test_list_performance.py` uses, and for the same
    reason: an N+1 is a property of the composed graph, and every layer of
    one has looked correct individually while the whole issued a query per
    row.
    """
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


class TestOrdering:
    async def test_it_lists_every_status_newest_first_and_nothing_operational(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§7, §2 and §5 in one, because they are one response.

        **Newest first**, by `created_at` then id — the order a lobby
        renders and the order the cursor continues in.

        **Every status**, completed and cancelled included. A lobby that hid
        finished tournaments would answer "what happened here?" with
        silence, and hiding them by default is the failure mode this asserts
        against rather than a preference.

        And **only safe fields**: the field set is compared exactly, so
        `created_by` — which every row here has — cannot appear because
        somebody added a column to the summary.
        """
        viewer = await register_account(client)
        newest = await _tournament(
            contract_session,
            name="Newest",
            created_at=NOW,
            status=TournamentStatus.CANCELLED,
        )
        middle = await _tournament(
            contract_session,
            name="Middle",
            created_at=NOW - timedelta(hours=1),
            status=TournamentStatus.COMPLETED,
        )
        oldest = await _tournament(
            contract_session,
            name="Oldest",
            created_at=NOW - timedelta(hours=2),
            status=TournamentStatus.DRAFT,
        )

        response = await client.get(LOBBY_URL, headers=viewer.auth)

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert [entry["id"] for entry in body["entries"]] == [
            str(newest.id),
            str(middle.id),
            str(oldest.id),
        ]
        assert set(body["entries"][0]) == PUBLIC_FIELDS
        assert body["next_cursor"] is None

    async def test_a_cursor_walks_the_lobby_without_repeating_or_skipping(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§4 — keyset, driven the way a client drives it.

        The cursor is read off one response and sent back unread, so this
        asserts the encoding round-trips as well as the predicate. Two of
        the five share an instant, so the `id` tie-break is exercised rather
        than merely present: a single-key order would show one of that pair
        twice and skip the other.

        The last page reports no further cursor, which is what `limit + 1`
        buys — a count alone would send a reader back for an empty page
        whenever the lobby's length is a multiple of the limit.
        """
        viewer = await register_account(client)
        instants = (
            NOW,
            NOW - timedelta(minutes=1),
            NOW - timedelta(minutes=1),  # a deliberate tie
            NOW - timedelta(minutes=2),
            NOW - timedelta(minutes=3),
        )
        created = [
            await _tournament(contract_session, name=f"T{index}", created_at=instant)
            for index, instant in enumerate(instants)
        ]
        expected = [
            str(tournament.id)
            for tournament in sorted(created, key=lambda t: (t.created_at, t.id), reverse=True)
        ]

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(3):
            page = await client.get(
                LOBBY_URL,
                params={"limit": 2, **({"after": cursor} if cursor else {})},
                headers=viewer.auth,
            )
            assert page.status_code == 200, page.text
            body = page.json()["data"]
            seen.extend(entry["id"] for entry in body["entries"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        assert seen == expected  # every one exactly once, in the published order
        assert cursor is None

    async def test_a_forged_cursor_is_refused_without_describing_the_encoding(
        self, client: AsyncClient
    ) -> None:
        """§9 — one error for every way a cursor can be wrong.

        A caller can do nothing differently for bad base64 than for an
        unparseable instant — the answer is always "ask for the first page"
        — and distinguishing them would narrate the encoding to whoever is
        probing it. No class name, no stack and no SQL in the response.
        """
        viewer = await register_account(client)

        response = await client.get(
            LOBBY_URL, params={"after": "not-a-cursor"}, headers=viewer.auth
        )

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "invalid_cursor"
        assert "Traceback" not in response.text
        assert "TournamentListCursor" not in response.text


class TestFilters:
    async def test_the_status_filter_narrows_and_an_unknown_value_is_refused(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§4 — the approved filters, and what an unsupported value does.

        Both halves in one test because they are one contract: `status` is
        a closed enum, so narrowing works and a value outside the enum is a
        `422` from validation rather than an empty page that looks like a
        state nobody is in. Silently ignoring it is the failure this rules
        out.
        """
        viewer = await register_account(client)
        open_now = await _tournament(
            contract_session,
            name="Open",
            created_at=NOW,
            status=TournamentStatus.REGISTRATION_OPEN,
        )
        await _tournament(
            contract_session,
            name="Done",
            created_at=NOW - timedelta(hours=1),
            status=TournamentStatus.COMPLETED,
        )

        narrowed = await client.get(
            LOBBY_URL, params={"status": "registration_open"}, headers=viewer.auth
        )
        refused = await client.get(
            LOBBY_URL, params={"status": "not_a_status"}, headers=viewer.auth
        )

        assert narrowed.status_code == 200, narrowed.text
        assert [entry["id"] for entry in narrowed.json()["data"]["entries"]] == [str(open_now.id)]
        assert refused.status_code == 422, refused.text


class TestCost:
    async def test_the_entrant_count_does_not_grow_the_query_count(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§6, §12 — the N+1 this endpoint would otherwise be.

        `entrant_count` and `current_round` are per-tournament numbers, and
        the obvious implementation asks for each of them per row: a page of
        40 would issue 81 queries and pass every test written against a page
        of one. So the assertion is that the count is **the same** for a
        page of 1 and a page of 40, which fails only when somebody has
        introduced a per-row read.

        An exact count is asserted too, because it is small enough to be
        meaningful and stating it is what makes a regression legible rather
        than merely detected.
        """
        viewer = await register_account(client)
        for index in range(40):
            await _tournament(
                contract_session,
                name=f"Field {index}",
                created_at=NOW - timedelta(minutes=index),
                entrants=index % 4,
            )

        one = await _statements_for(
            contract_session,
            lambda: client.get(LOBBY_URL, params={"limit": 1}, headers=viewer.auth),
        )
        forty = await _statements_for(
            contract_session,
            lambda: client.get(LOBBY_URL, params={"limit": 40}, headers=viewer.auth),
        )

        assert one == forty == 1

        page = await client.get(LOBBY_URL, params={"limit": 40}, headers=viewer.auth)
        counts = [entry["entrant_count"] for entry in page.json()["data"]["entries"]]
        assert counts[:4] == [0, 1, 2, 3]  # and the numbers are right, not merely cheap


class TestReachability:
    async def test_the_lobby_is_registered_on_the_real_v1_router(
        self, contract_session: AsyncSession
    ) -> None:
        """§10 — the route ships, and the operator commands still do not.

        Asserted against the application `create_app()` builds rather than
        against the module's own `APIRouter`, because a router nothing
        includes is exactly the gap A64-019.7's audit found: every write use
        case was implemented, tested, and reachable from nothing.

        The second half is the audit's guard, restated for this phase: the
        lobby is a read, and adding it must not have opened a path to
        creating a tournament over HTTP.
        """
        paths = build_contract_app(contract_session).openapi()["paths"]

        assert list(paths["/api/v1/tournaments"]) == ["get"]
        assert {
            (path, method)
            for path, methods in paths.items()
            if path.startswith("/api/v1/tournaments")
            for method in methods
            if method != "get"
        } == {
            ("/api/v1/tournaments/{tournament_id}/registrations", "post"),
            ("/api/v1/tournaments/{tournament_id}/registrations/me", "delete"),
        }
