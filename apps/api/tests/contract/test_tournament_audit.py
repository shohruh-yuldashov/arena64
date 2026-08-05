"""A64-019.7 — the two things no existing tournament suite asserts.

Every other tournament test proves one layer. These two prove the **seams
between** them, which is where this epic's defects have actually been:

    the whole flow      registration to published results, in one run,
                        through the production services and the real HTTP
                        router. Each phase tested its own step; nothing
                        tested that step 7 hands step 8 what it needs
    the wiring          every background entry point named by the
                        composition root, and every read route on the real
                        v1 router — plus a written record of the write path
                        that is deliberately **not** reachable

The rest of §17's suggested list is deliberately absent. The draw policy,
the no-show policy, the placement tiers and the HTTP responses each already
have a real PostgreSQL test in `test_tournament_matches.py` or
`test_tournament_results.py`; repeating them here would be the duplicate
test layer the audit brief forbids, and would make the suite slower without
covering a single new line.

Skipped, not failed, when PostgreSQL is unreachable.
"""

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.game.public import PlayerSide, ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.application.ports import (
    AlreadyRegistered,
    TournamentIsFull,
)
from app.modules.tournament.domain.attempts import AdvancementReason, AttemptOutcome
from app.modules.tournament.domain.standings import FinalStatus
from app.modules.tournament.domain.tournament import TournamentStatus
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyBracketRepository,
    SqlAlchemyPairingAttemptRepository,
    SqlAlchemyStandingRepository,
    SqlAlchemyTournamentRepository,
)
from app.operator import tournament as operator_tournament
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register as register_account
from tests.contract.test_tournament_results import (
    NOW,
    _completion,
    _consumer,
    _KnownPlayers,
    _play_out,
    _registration,
    _start,
)
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import RecordingPublisher
from tests.unit.test_auth_rate_limits import api_routes

_APP = Path(__file__).resolve().parents[2] / "app"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


class TestTheWholeTournamentRuns:
    async def test_registration_to_published_results_in_one_run(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§1 — the twenty-two steps, end to end, with nothing stubbed.

        Every phase of this epic tested its own step against a real
        database. What none of them tested is that the steps **compose**:
        that seeding hands materialisation a field it can build a tree from,
        that starting hands the consumer matches it recognises, that a draw
        mid-tournament does not derail the round that follows, and that the
        result a player finally reads over HTTP is the one the bracket
        produced.

        Eight players, so the bracket has three rounds and a full set of
        placement tiers, and one first-round pairing draws twice — so the
        run exercises the rematch, the seed tie-break and an ordinary
        decisive round in a single tournament rather than three.
        """
        clock = MovableClock(NOW)
        players = [uuid4() for _ in range(8)]
        directory = _KnownPlayers(*players, uuid4())
        registration = _registration(contract_session, directory, clock)

        # 1–2. Created by the system, then opened.
        tournament = await registration.create(
            name="Audit Open",
            variant=ProductVariant.RUSSIAN_8X8,
            speed_class=SpeedClass.CLASSICAL,
            capacity=8,
        )
        assert tournament.status is TournamentStatus.DRAFT
        tournament = await registration.open_registration(tournament.id)

        # 3–4. Players enter, and the two refusals are checked in the order
        # the guards run: `add` counts inside the lock *before* it inserts,
        # so a duplicate is only reported as a duplicate while a slot
        # remains. Both are refusals a client answers the same way, and the
        # ordering is recorded rather than treated as a defect.
        for player in players[:-1]:
            await registration.register(tournament.id, player)
        with pytest.raises(AlreadyRegistered):
            await registration.register(tournament.id, players[0])

        await registration.register(tournament.id, players[-1])
        with pytest.raises(TournamentIsFull):
            await registration.register(tournament.id, next(iter(directory.known - set(players))))
        assert await registration.entrant_count(tournament.id) == 8

        # 5–10. Close, seed, materialise, start.
        await registration.close_registration(tournament.id)
        attempts_repo = SqlAlchemyPairingAttemptRepository(contract_session)
        launched = await _seed_and_start(contract_session, clock, tournament.id)
        assert len(launched) == 4  # a full round of eight players

        # 11. Every launched match carries the tournament's provenance and
        # no fabricated queue ticket.
        for attempt in launched:
            assert attempt.no_show_deadline is not None
            record = await _match_row(contract_session, attempt.match_id)
            assert record.origin.value == "tournament"
            assert record.origin_ref == attempt.pairing_id
            assert record.light_ticket_id is None and record.dark_ticket_id is None
            assert record.light_rating_value is not None  # the seat snapshot
            assert record.status.value == "active"  # system-activated

        # 12. Attendance is recorded through the port the gateway holds.
        assert await attempts_repo.mark_present(
            launched[0].match_id, launched[0].light_player_id, at=NOW
        )

        # 14–17. One pairing draws twice: exactly one side-swapped rematch,
        # then the higher seed advances by adjudication.
        drawn = launched[0]
        consumer = _consumer(contract_session, clock)
        await consumer.handle(
            _completion(match_id=drawn.match_id, pairing_id=drawn.pairing_id, winner=None)
        )
        rematch = await attempts_repo.latest_for(drawn.pairing_id)
        assert rematch is not None
        assert rematch.attempt_number == 2
        assert (rematch.light_player_id, rematch.dark_player_id) == (
            drawn.dark_player_id,
            drawn.light_player_id,
        )
        await consumer.handle(
            _completion(match_id=rematch.match_id, pairing_id=rematch.pairing_id, winner=None)
        )
        assert len(await attempts_repo.for_pairings([drawn.pairing_id])) == 2  # no third

        bracket = SqlAlchemyBracketRepository(contract_session)
        adjudicated = next(
            n for n in await bracket.nodes_for(tournament.id) if n.id == drawn.pairing_id
        )
        assert adjudicated.advancement_reason is AdvancementReason.ADJUDICATION

        # 18–20. The rest is played out; rounds publish and the final
        # completes the tournament without anybody asking it to.
        await _play_out(contract_session, clock, tournament.id)

        # 21. Immutable standings, materialised in the completing transaction.
        stored = await SqlAlchemyTournamentRepository(contract_session).by_id(tournament.id)
        assert stored is not None
        assert stored.status is TournamentStatus.COMPLETED
        assert stored.completed_at is not None
        standings = await SqlAlchemyStandingRepository(contract_session).standings_for(
            tournament.id
        )
        assert [s.final_rank for s in standings] == [1, 2, 3, 3, 5, 5, 5, 5]
        assert standings[0].final_status is FinalStatus.CHAMPION

        # The adjudicated advancement is counted as one, and as no win.
        advanced = adjudicated.winner_id
        by_player = {s.player_id: s for s in standings}
        assert by_player[advanced].adjudicated_advancements >= 1
        assert by_player[advanced].draws == 2
        # No `game` result was fabricated for it.
        settled = await attempts_repo.by_match(rematch.match_id)
        assert settled is not None and settled.outcome is AttemptOutcome.DRAW

        # 22. The public surface serves what the bracket produced.
        viewer = await register_account(client)
        detail = await client.get(f"/api/v1/tournaments/{tournament.id}", headers=viewer.auth)
        standings_body = await client.get(
            f"/api/v1/tournaments/{tournament.id}/standings", headers=viewer.auth
        )
        bracket_body = await client.get(
            f"/api/v1/tournaments/{tournament.id}/bracket", headers=viewer.auth
        )
        history = await client.get(
            f"/api/v1/players/{standings[0].player_id}/tournaments", headers=viewer.auth
        )

        assert detail.json()["data"]["status"] == "completed"
        assert [r["final_rank"] for r in standings_body.json()["data"]["standings"]] == [
            1,
            2,
            3,
            3,
            5,
            5,
            5,
            5,
        ]
        assert len(bracket_body.json()["data"]["rounds"]) == 3
        assert history.json()["data"]["entries"][0]["final_rank"] == 1


class TestEveryTournamentEntryPointIsWired:
    """§2 — the defect class that has appeared in three epics.

    `tests/unit/test_reachability.py` catches an unwired `handle`/`run`/
    `consume`. It cannot catch a *route* that was written and never included
    in a router, or a use case that has no caller at all — and both are the
    same failure: complete, correct, tested code the runtime cannot reach.
    """

    async def test_every_public_read_route_is_on_the_real_v1_router(
        self, contract_session: AsyncSession
    ) -> None:
        """A route file that is never included is a 404 nobody notices.

        Asserted against the **assembled application** rather than the
        module's own `APIRouter`, because including it in `v1_router` is the
        step that can be forgotten.

        Walked with `api_routes` rather than by reading `app.routes`: this
        FastAPI keeps an included router as one opaque entry instead of
        flattening it, so the obvious one-level loop finds nothing — and a
        reachability assertion that finds nothing passes vacuously, which is
        the worst outcome available to this test.
        """
        paths = {path for path, _ in api_routes(build_contract_app(contract_session))}

        assert {
            "/api/v1/tournaments/{tournament_id}",
            "/api/v1/tournaments/{tournament_id}/bracket",
            "/api/v1/tournaments/{tournament_id}/standings",
            "/api/v1/players/{player_id}/tournaments",
        } <= paths

    def test_every_tournament_background_entry_point_is_named_by_app_factory(self) -> None:
        """Each of the four has a handler **and** a schedule or a consumer.

        Naming the class is what `test_reachability.py` already asserts.
        What this adds is the other half: a `TaskHandler` registered without
        a `PeriodicTaskScheduler` never runs, and a consumer registered
        without an event type never wakes — neither of which the structural
        check can see.
        """
        root = (_APP / "app_factory.py").read_text(encoding="utf-8")

        for handler, request in (
            ("TournamentDeadlineTask", "tournament_deadline_request"),
            ("TournamentReconciliationTask", "tournament_reconciliation_request"),
            ("TournamentNoShowTask", "tournament_no_show_request"),
        ):
            assert re.search(rf"\b{handler}\(", root), handler
            assert re.search(rf"\b{request}\(\)", root), request

        assert "TOURNAMENT_CONSUMER" in root
        assert "_tournament_consumer_for" in root

    async def test_every_write_use_case_has_a_production_entry_point(
        self, contract_session: AsyncSession
    ) -> None:
        """**A64-019.8 closed the audit's headline finding**, and this is now
        the assertion that keeps it closed.

        Until A64-019.8 this test asserted the opposite — that creating,
        opening, closing, seeding, starting, registering and withdrawing were
        reachable from nothing — so that the gap could not go stale and
        silently become the A64-017.6 defect, where a whole module was built
        and never called.

        It now asserts the two surfaces that replaced it: participant writes
        on the real v1 router, and operator writes on the process entry point
        `python -m app.operator.tournament`. A future change that removes
        either fails here.
        """
        paths = {path for path, _ in api_routes(build_contract_app(contract_session))}
        assert {
            "/api/v1/tournaments/{tournament_id}/registrations",
            "/api/v1/tournaments/{tournament_id}/registrations/me",
        } <= paths

        for command in ("create", "open_registration", "close_registration", "seed", "start"):
            assert callable(getattr(operator_tournament, command)), command

    async def test_no_operator_command_is_exposed_over_http(
        self, contract_session: AsyncSession
    ) -> None:
        """The other half, and the one that matters for security.

        This platform has no administrator — no role on `users.User`, no
        scope on `auth.TokenClaims`, no permission primitive. So the
        lifecycle commands must be reachable by a **process** and by nothing
        an authenticated player can send, and the strongest way to hold that
        is for no such route to exist at all.

        Asserted over the whole route table rather than by trying a few
        paths, so a route added under any prefix is caught.
        """
        paths = {path for path, _ in api_routes(build_contract_app(contract_session))}

        assert not [path for path in paths if "/admin" in path]
        assert not [
            path
            for path in paths
            if path.startswith("/api/v1/tournaments")
            and any(command in path for command in ("/seed", "/start", "/registration/"))
        ]

    def test_no_tournament_service_defines_an_unreachable_entry_point(self) -> None:
        """No `handle`/`run`/`consume` in this module escapes the registry.

        A narrower restatement of `test_reachability.py`, scoped to
        `tournament` and asserted by name, so a future phase that adds a
        fifth handler and forgets `app_factory` fails here with the module's
        own list rather than in a platform-wide diff.
        """
        entry_points: set[str] = set()
        for path in (_APP / "modules" / "tournament").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for item in node.body:
                    if not isinstance(item, ast.AsyncFunctionDef):
                        continue
                    parameters = [argument.arg for argument in item.args.args]
                    if (item.name, "entries") in {
                        ("handle", "entries")
                    } and "entries" in parameters:
                        entry_points.add(node.name)
                    if item.name == "run" and "payload" in parameters:
                        entry_points.add(node.name)

        assert entry_points == {
            "TournamentDeadlineTask",
            "TournamentNoShowTask",
            "TournamentReconciliationTask",
            "TournamentMatchCompletionConsumer",
        }

        root = (_APP / "app_factory.py").read_text(encoding="utf-8")
        for name in entry_points:
            assert re.search(rf"\b{name}\b", root), f"{name} is not wired"


async def _seed_and_start(session: AsyncSession, clock: MovableClock, tournament_id: UUID):  # type: ignore[no-untyped-def]
    """Seeding, materialisation and the start, through the production graph."""
    from app.database.unit_of_work import SessionUnitOfWork
    from app.modules.rating.infrastructure.repositories.player_rating_repository import (
        SqlAlchemyRatingReader,
    )
    from app.modules.tournament.application.services.seeding_service import (
        TournamentSeedingService,
    )
    from app.modules.tournament.infrastructure.rating_snapshots import (
        PublishedRatingSnapshots,
    )
    from app.modules.tournament.infrastructure.repositories.tournament_repository import (
        SqlAlchemyPairingRepository,
        SqlAlchemySeedRepository,
    )

    await TournamentSeedingService(
        tournaments=SqlAlchemyTournamentRepository(session),
        seeds=SqlAlchemySeedRepository(session),
        pairings=SqlAlchemyPairingRepository(session),
        ratings=PublishedRatingSnapshots(SqlAlchemyRatingReader(session)),
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    ).seed_tournament(tournament_id)
    return await _start(session, clock).start_tournament(tournament_id)


async def _match_row(session: AsyncSession, match_id: UUID):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from app.modules.game.infrastructure.models import MatchRecordModel

    row = await session.scalar(select(MatchRecordModel).where(MatchRecordModel.id == match_id))
    assert row is not None
    return row


_ = (datetime, UTC, PlayerSide)  # imported for the shared helpers' signatures
