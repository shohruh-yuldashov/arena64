"""The tournament write paths, through the paths production uses — A64-019.8.

The audit's headline finding was that every tournament write use case was
implemented, tested, and reachable from nothing. These tests exist to make
that permanently false, so they are deliberately *not* service tests:

    participants   real v1 router, real session, real `CurrentUser`
    operators      `app.operator.tournament`'s own functions, over a real
                   session — the same entry point `python -m` runs

A test that constructed `TournamentRegistrationService` itself would prove
the service works, which every earlier phase already did. What was missing
is that something *reaches* it.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.modules.game.public import ProductVariant
from app.modules.tournament.domain.tournament import Tournament, TournamentStatus
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyPairingAttemptRepository,
    SqlAlchemyTournamentRepository,
)
from app.operator import tournament as operator
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register as register_account

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def _open_tournament(
    session: AsyncSession, *, capacity: int = 8, deadline: datetime | None = None
) -> Tournament:
    """A tournament open for entries, created through the **operator entry
    point** — so every test here exercises both surfaces at once."""
    settings = get_settings()
    tournament = await operator.create(
        session,
        settings,
        name="Entry Points Open",
        variant=ProductVariant.RUSSIAN_8X8,
        capacity=capacity,
        rated=True,
        registration_deadline=deadline,
        created_by=None,
    )
    return await operator.open_registration(session, settings, tournament.id)


class TestParticipantEndpoints:
    async def test_a_player_enters_themselves_through_the_real_router(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§1, §9 — the route the audit said did not exist.

        Reached over HTTP, so the router registration, the `Depends`
        factory, the service and the repository are all the ones that ship.
        The response carries the player from `CurrentUser`, never from a
        body — there is no body.
        """
        tournament = await _open_tournament(contract_session)
        player = await register_account(client, contract_session)

        created = await client.post(
            f"/api/v1/tournaments/{tournament.id}/registrations", headers=player.auth
        )

        assert created.status_code == 201, created.text
        body = created.json()["data"]
        assert body["player_id"] == str(player.id)
        assert body["tournament_id"] == str(tournament.id)
        assert body["status"] == "registered"
        assert body["tournament_status"] == "registration_open"
        assert body["seed_number"] is None  # not seeded yet
        assert body["withdrawn_at"] is None

    async def test_a_duplicate_entry_is_a_stable_conflict(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§7 — the code, not the constraint name.

        The refusal comes from `pk_registration`, and what reaches the
        client is `already_registered` with no SQL, no index name and no
        Python class in sight. A client reconciling a dropped response
        branches on this code and treats it as success.
        """
        tournament = await _open_tournament(contract_session)
        player = await register_account(client, contract_session)
        url = f"/api/v1/tournaments/{tournament.id}/registrations"

        assert (await client.post(url, headers=player.auth)).status_code == 201
        duplicate = await client.post(url, headers=player.auth)

        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "already_registered"
        assert "pk_registration" not in duplicate.text
        assert "IntegrityError" not in duplicate.text

    async def test_a_full_tournament_and_a_passed_deadline_are_refused_distinctly(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§1, §7 — two refusals a client answers differently.

        The capacity guard is the existing one, checked inside the row
        lock; the deadline guard is A64-019.8's, and it is the reason a
        player cannot beat `TournamentDeadlineTask` by a few seconds — the
        deadline is the promise, not the worker's tick.

        Asserted together because the point is that they are **not the same
        code**: "the field is full" offers another tournament, and
        "registration closed at 14:00" says when.
        """
        full = await _open_tournament(contract_session, capacity=2)
        first, second, third = [await register_account(client, contract_session) for _ in range(3)]
        url = f"/api/v1/tournaments/{full.id}/registrations"
        assert (await client.post(url, headers=first.auth)).status_code == 201
        assert (await client.post(url, headers=second.auth)).status_code == 201

        overflow = await client.post(url, headers=third.auth)
        assert overflow.status_code == 409
        assert overflow.json()["code"] == "tournament_full"

        # The deadline has already passed when the entry arrives, and the
        # sweep has not run — the status still says open.
        overdue = await _open_tournament(
            contract_session, deadline=datetime.now(UTC) - timedelta(minutes=1)
        )
        assert overdue.status is TournamentStatus.REGISTRATION_OPEN
        late = await client.post(
            f"/api/v1/tournaments/{overdue.id}/registrations", headers=first.auth
        )

        assert late.status_code == 409
        assert late.json()["code"] == "registration_deadline_passed"

    async def test_a_player_withdraws_their_own_entry_and_cannot_do_it_twice(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§1 — `/me`, and the row that survives.

        The registration is not deleted: its status becomes `withdrawn` and
        `withdrawn_at` is set, so "who was in this tournament" stays
        answerable. A second withdrawal answers `404 registration_not_found`
        — the same answer as never having entered, which is what makes the
        call safe to send twice without `withdrawn_at` moving.
        """
        tournament = await _open_tournament(contract_session)
        player = await register_account(client, contract_session)
        await client.post(f"/api/v1/tournaments/{tournament.id}/registrations", headers=player.auth)

        withdrawn = await client.delete(
            f"/api/v1/tournaments/{tournament.id}/registrations/me", headers=player.auth
        )

        assert withdrawn.status_code == 200, withdrawn.text
        body = withdrawn.json()["data"]
        assert body["status"] == "withdrawn"
        assert body["withdrawn_at"] is not None
        assert body["player_id"] == str(player.id)

        repeated = await client.delete(
            f"/api/v1/tournaments/{tournament.id}/registrations/me", headers=player.auth
        )
        assert repeated.status_code == 404
        assert repeated.json()["code"] == "registration_not_found"

    async def test_withdrawal_after_registration_closes_is_refused(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§1 — after close the field is fixed.

        The bracket is built from exactly those players, so a withdrawal
        would leave a seat nothing fills. Refused rather than converted to
        a forfeit: a forfeit is a *match* outcome and there is no match yet.
        """
        tournament = await _open_tournament(contract_session)
        player = await register_account(client, contract_session)
        await client.post(f"/api/v1/tournaments/{tournament.id}/registrations", headers=player.auth)
        await operator.close_registration(contract_session, get_settings(), tournament.id)

        refused = await client.delete(
            f"/api/v1/tournaments/{tournament.id}/registrations/me", headers=player.auth
        )

        assert refused.status_code == 409
        assert refused.json()["code"] == "registration_not_open"

    async def test_an_ordinary_user_cannot_reach_any_operator_command(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§6 — the tournament lifecycle commands are not on the HTTP router.

        Seeding, starting and closing registration are a **process** entry
        point (`app/operator/`), so the strongest possible statement holds:
        there is no request an authenticated player can send that reaches
        them. A route that does not exist is the only authorization that
        cannot be misconfigured.

        ## Why `/admin/tournaments` is asserted differently now

        When this test was written the admin console did not exist and
        nothing answered under `/admin`. A64-024.5 added a **read-only**
        console there, and until the prefix defect was fixed its routes were
        mounted at `/api/v1/v1/admin/...` — so this assertion kept passing
        for the wrong reason.

        A64-024.5 added a read-only listing there and A64-024.5H added the
        lifecycle commands, so `/api/v1/admin/tournaments` and
        `/api/v1/admin/tournaments/{id}/start` are now real routes behind
        `CurrentAdmin`. The claim they can support has changed with them: not
        "nothing answers", but "the guard refuses a player" — `403`. Asserting
        `404` or `405` there today would be asserting that the console had
        not shipped.

        The player's own router is where the original claim still holds, and
        it is the one this test is really about.
        """
        tournament = await _open_tournament(contract_session)
        player = await register_account(client, contract_session)

        # Still nothing at all on the player's own router: seeding, starting
        # and closing remain a process entry point, and a route that does
        # not exist is the only authorization that cannot be misconfigured.
        for path in (
            f"/api/v1/tournaments/{tournament.id}/seed",
            f"/api/v1/tournaments/{tournament.id}/start",
            f"/api/v1/tournaments/{tournament.id}/registration/close",
        ):
            answered = await client.post(path, headers=player.auth)
            assert answered.status_code == 404, f"{path} answered {answered.status_code}"

        # A64-024.5H put `start` on the admin router, so this one is now a
        # real route and the claim it supports changes: it is no longer
        # "nobody can reach this" but "an ordinary player is refused". The
        # weaker statement is the true one, and asserting `404` here would
        # be asserting that the feature had not shipped.
        assert (
            await client.post(
                f"/api/v1/admin/tournaments/{tournament.id}/start", headers=player.auth
            )
        ).status_code == 403

        # Creation is an admin command since A64-024.5H, so this is the
        # guard refusing a player rather than the router refusing a method.
        assert (
            await client.post("/api/v1/admin/tournaments", headers=player.auth)
        ).status_code == 403

        # And the read is not a player's to make either.
        assert (
            await client.get("/api/v1/admin/tournaments", headers=player.auth)
        ).status_code == 403


class TestOperatorEntryPoint:
    async def test_it_creates_opens_closes_seeds_and_starts_a_tournament(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§3, §5, §9 — the whole operator lifecycle, through `app.operator`.

        The functions called here are the ones `python -m
        app.operator.tournament` dispatches to; only the argument parsing
        and the session manager are skipped, because a subprocess would run
        against a different transaction from the one this test rolls back.

        Each step is asserted to be **idempotent**, because an operator
        retrying a command that timed out is the ordinary case: opening
        twice reports the state, seeding twice returns the persisted plan,
        and starting twice creates no second match.
        """
        settings = get_settings()
        tournaments = SqlAlchemyTournamentRepository(contract_session)

        created = await operator.create(
            contract_session,
            settings,
            name="Operator Open",
            variant=ProductVariant.RUSSIAN_8X8,
            capacity=4,
            rated=True,
            registration_deadline=None,
            created_by=None,
        )
        assert created.status is TournamentStatus.DRAFT

        opened = await operator.open_registration(contract_session, settings, created.id)
        assert opened.status is TournamentStatus.REGISTRATION_OPEN
        # Idempotent: the second call reports rather than raising.
        assert (
            await operator.open_registration(contract_session, settings, created.id)
        ).status is TournamentStatus.REGISTRATION_OPEN

        players = [await register_account(client, contract_session) for _ in range(4)]
        for player in players:
            entered = await client.post(
                f"/api/v1/tournaments/{created.id}/registrations", headers=player.auth
            )
            assert entered.status_code == 201, entered.text

        closed = await operator.close_registration(contract_session, settings, created.id)
        assert closed.status is TournamentStatus.REGISTRATION_CLOSED
        assert (
            await operator.close_registration(contract_session, settings, created.id)
        ).status is TournamentStatus.REGISTRATION_CLOSED

        assert await operator.seed(contract_session, settings, created.id) == 2
        assert await operator.seed(contract_session, settings, created.id) == 2

        assert await operator.start(contract_session, settings, created.id) == 2
        assert await operator.start(contract_session, settings, created.id) == 2

        running = await tournaments.by_id(created.id)
        assert running is not None
        assert running.status is TournamentStatus.IN_PROGRESS
        assert running.started_at is not None

        # The matches are real, carry the tournament's provenance, and
        # nobody fabricated a queue ticket for them.
        attempts = SqlAlchemyPairingAttemptRepository(contract_session)
        seeded = await client.get(
            f"/api/v1/tournaments/{created.id}/bracket", headers=players[0].auth
        )
        assert seeded.status_code == 200
        launched = [
            attempt
            for round_ in seeded.json()["data"]["rounds"]
            for node in round_["nodes"]
            for attempt in node["attempts"]
        ]
        assert len(launched) == 2
        for attempt in launched:
            stored = await attempts.by_match(UUID(attempt["match_id"]))
            assert stored is not None
            assert stored.no_show_deadline is not None

    def test_the_command_line_exposes_every_lifecycle_command(self) -> None:
        """§9 — the surface `python -m app.operator.tournament` presents.

        The parser is asserted rather than the process, because spawning
        one would run against a different transaction from the one this
        suite rolls back. What matters here is that all five commands are
        reachable from the command line at all — a function nothing
        dispatches to is the defect this whole task exists to remove.
        """
        parser = operator._parser()
        commands = next(action for action in parser._actions if getattr(action, "choices", None))

        assert commands.choices is not None
        assert set(commands.choices) == {"create", "open", "close", "seed", "start", "run"}
