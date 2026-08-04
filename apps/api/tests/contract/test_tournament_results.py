"""Final results, against real PostgreSQL and the real router — A64-019.6 §19.

Eight tests. Each asserts something no unit test can: what the completion
transaction actually commits, what the placement is for a real eight-player
bracket, what a duplicate completion does, and that the four read endpoints
are reachable through the v1 router rather than merely written.

The tournament is driven through its **production** services — the same
graph `app_factory` assembles — so a placement, a statistic and an endpoint
are all proven against the path that ships. The bracket is played out by
feeding real `game.match_completed` payloads to the real outbox consumer,
which is what makes the automatic completion trigger (§14) observable: no
test calls `complete_tournament` for the tournament that finishes normally.

What is deliberately **not** re-tested here: the draw policy, the no-show
policy and the bracket's own advancement. Those are A64-019.5's and
A64-019.5H's suites, and duplicating them would grow the tournament suite
without covering anything new.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import TournamentSettings
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.domain.events import MatchCompleted
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import MatchOrigin
from app.modules.game.public import PlayerSide, ProductVariant
from app.modules.matchmaking.presentation.dependencies import build_match_creation
from app.modules.rating.infrastructure.repositories.player_rating_repository import (
    SqlAlchemyRatingReader,
)
from app.modules.rating.public import SpeedClass
from app.modules.tournament.application.services.bracket_service import (
    TournamentBracketService,
)
from app.modules.tournament.application.services.match_completion_consumer import (
    TournamentMatchCompletionConsumer,
)
from app.modules.tournament.application.services.match_launcher import (
    TournamentMatchLauncher,
)
from app.modules.tournament.application.services.registration_service import (
    TournamentRegistrationService,
)
from app.modules.tournament.application.services.seeding_service import (
    TournamentSeedingService,
)
from app.modules.tournament.application.services.start_service import (
    TournamentStartService,
)
from app.modules.tournament.domain.attempts import AdvancementReason
from app.modules.tournament.domain.standings import FinalStatus
from app.modules.tournament.domain.tournament import TournamentStatus
from app.modules.tournament.infrastructure.rating_snapshots import (
    PublishedRatingSnapshots,
)
from app.modules.tournament.infrastructure.repositories.results_repository import (
    SqlAlchemyTournamentResults,
)
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyBracketRepository,
    SqlAlchemyPairingAttemptRepository,
    SqlAlchemyPairingRepository,
    SqlAlchemyRegistrationRepository,
    SqlAlchemyRoundRepository,
    SqlAlchemySeedRepository,
    SqlAlchemyStandingRepository,
    SqlAlchemyTournamentRepository,
)
from app.modules.tournament.presentation.dependencies import (
    build_advancement_service,
    build_completion_service,
)
from app.platform.outbox import OutboxEntry
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import RecordingPublisher

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
NO_SHOW_SECONDS = 300

_SETTINGS = TournamentSettings(no_show_seconds=NO_SHOW_SECONDS)


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):
    """The real API over this suite's session — §17's requirement.

    A route file that exists without router registration is incomplete, and
    only a request that reaches it proves otherwise.
    """
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


class _KnownPlayers:
    """A `PlayerDirectory` a test dictates — the seam every tournament suite
    uses, so `users` is not a dependency of a placement test."""

    def __init__(self, *known: UUID) -> None:
        self.known = set(known)

    async def get_profile(self, user_id: UUID) -> object:
        if user_id not in self.known:
            raise LookupError(f"no such player {user_id}")
        return object()


def _registration(
    session: AsyncSession, players: _KnownPlayers, clock: MovableClock
) -> TournamentRegistrationService:
    return TournamentRegistrationService(
        tournaments=SqlAlchemyTournamentRepository(session),
        registrations=SqlAlchemyRegistrationRepository(session),
        players=players,
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def _start(session: AsyncSession, clock: MovableClock) -> TournamentStartService:
    return TournamentStartService(
        tournaments=SqlAlchemyTournamentRepository(session),
        brackets=TournamentBracketService(
            tournaments=SqlAlchemyTournamentRepository(session),
            seeds=SqlAlchemySeedRepository(session),
            pairings=SqlAlchemyPairingRepository(session),
            bracket=SqlAlchemyBracketRepository(session),
            events=RecordingPublisher(),
            unit_of_work=SessionUnitOfWork(session),
            clock=clock,
        ),
        bracket=SqlAlchemyBracketRepository(session),
        rounds=SqlAlchemyRoundRepository(session),
        attempts=SqlAlchemyPairingAttemptRepository(session),
        launcher=TournamentMatchLauncher(
            matches=build_match_creation(session, events=RecordingPublisher(), clock=clock),
            ratings=PublishedRatingSnapshots(SqlAlchemyRatingReader(session)),
            attempts=SqlAlchemyPairingAttemptRepository(session),
            clock=clock,
            no_show_seconds=NO_SHOW_SECONDS,
        ),
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def _consumer(session: AsyncSession, clock: MovableClock) -> TournamentMatchCompletionConsumer:
    """The production consumer graph — the path that reaches completion.

    This is what makes §14's automatic trigger observable: nothing in the
    normal flow below calls `complete_tournament`, and the tournament
    finishes anyway.
    """
    return TournamentMatchCompletionConsumer(
        build_advancement_service(
            session,
            matches=build_match_creation(session, events=RecordingPublisher(), clock=clock),
            settings=_SETTINGS,
            events=RecordingPublisher(),
            clock=clock,
        )
    )


def _completion(*, match_id: UUID, pairing_id: UUID, winner: PlayerSide | None):  # type: ignore[no-untyped-def]
    """One `game.match_completed`, built from the real event.

    `MatchCompleted.payload()` rather than a hand-written dict, so a field
    renamed in `game` breaks this suite instead of silently making the
    consumer skip every entry.
    """
    event = MatchCompleted(
        occurred_at=NOW,
        match_id=match_id,
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        outcome=MatchOutcome.WIN if winner is not None else MatchOutcome.DRAW,
        termination_reason=(
            TerminationReason.NO_LEGAL_MOVES if winner is not None else TerminationReason.REPETITION
        ),
        winner=winner,
        ply_number=30,
        origin=MatchOrigin.TOURNAMENT,
        origin_ref=pairing_id,
    )
    return [OutboxEntry.of(event)]


async def _seeded_tournament(
    session: AsyncSession, clock: MovableClock, *, entrants: int, capacity: int, players=None
):  # type: ignore[no-untyped-def]
    """A tournament registered, seeded, bracketed and started."""
    field = players or [uuid4() for _ in range(entrants)]
    directory = _KnownPlayers(*field)
    registration = _registration(session, directory, clock)

    tournament = await registration.create(
        name="Sunday Open",
        variant=ProductVariant.RUSSIAN_8X8,
        speed_class=SpeedClass.CLASSICAL,
        capacity=capacity,
    )
    tournament = await registration.open_registration(tournament.id)
    for player in field:
        await registration.register(tournament.id, player)
    await registration.close_registration(tournament.id)

    await TournamentSeedingService(
        tournaments=SqlAlchemyTournamentRepository(session),
        seeds=SqlAlchemySeedRepository(session),
        pairings=SqlAlchemyPairingRepository(session),
        ratings=PublishedRatingSnapshots(SqlAlchemyRatingReader(session)),
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    ).seed_tournament(tournament.id)

    await _start(session, clock).start_tournament(tournament.id)
    return tournament, field


async def _play_out(
    session: AsyncSession,
    clock: MovableClock,
    tournament_id: UUID,
    *,
    winner_of=None,
) -> None:
    """Plays every match to a decisive result until nothing is left.

    `winner_of(attempt)` chooses the winning **seat** for one attempt and
    defaults to the light player, which — because the higher seed takes the
    light seat on even slots (§6a) — makes the top seed the champion of an
    unperturbed bracket.

    Loops rather than walking rounds, because publishing the next round is
    itself a consequence of a completion: each pass plays whatever the last
    one created, and the tournament finishes when a pass creates nothing.
    """
    consumer = _consumer(session, clock)
    attempts = SqlAlchemyPairingAttemptRepository(session)
    bracket = SqlAlchemyBracketRepository(session)

    for _ in range(10):  # a 128-field bracket is 7 rounds; 10 is a runaway guard
        nodes = await bracket.nodes_for(tournament_id)
        live = [
            attempt
            for node in nodes
            if node.id is not None
            for attempt in await attempts.for_pairings([node.id])
            if attempt.outcome is None
        ]
        if not live:
            return

        for attempt in live:
            side = winner_of(attempt) if winner_of else PlayerSide.LIGHT
            await consumer.handle(
                _completion(match_id=attempt.match_id, pairing_id=attempt.pairing_id, winner=side)
            )


class TestCompletion:
    async def test_the_final_winner_completes_the_tournament_atomically(
        self, contract_session: AsyncSession
    ) -> None:
        """§5, §14 — the automatic trigger, and the state it must not leave.

        Nothing here calls `complete_tournament`. The bracket is played out
        through the **real outbox consumer**, and the tournament finishes
        because its final gained a winner — which is §14's "do not require an
        operator to finish a completed bracket manually".

        The atomicity assertion is the pair: `COMPLETED` **and** standings.
        A tournament that reached one without the other is the state §5
        names as impossible, and it would be unrepairable — `COMPLETED` is
        terminal, so nothing would run again to write the other half.
        """
        clock = MovableClock(NOW)
        tournament, players = await _seeded_tournament(
            contract_session, clock, entrants=4, capacity=4
        )

        await _play_out(contract_session, clock, tournament.id)

        stored = await SqlAlchemyTournamentRepository(contract_session).by_id(tournament.id)
        assert stored is not None
        assert stored.status is TournamentStatus.COMPLETED
        assert stored.completed_at == NOW
        assert stored.started_at == NOW

        standings = await SqlAlchemyStandingRepository(contract_session).standings_for(
            tournament.id
        )
        assert len(standings) == len(players)
        assert standings[0].final_rank == 1
        assert standings[0].final_status is FinalStatus.CHAMPION
        assert standings[0].elimination_round is None
        assert standings[0].eliminated_by_player_id is None
        assert standings[1].final_status is FinalStatus.RUNNER_UP

    async def test_the_placement_tiers_are_correct_for_eight_players(
        self, contract_session: AsyncSession
    ) -> None:
        """§2 — the placement formula, on the bracket the brief names.

        Eight players, three rounds:

            champion            1
            final loser         2
            semi-final losers   3, 3
            quarter losers      5, 5, 5, 5

        **Ties are not broken**, so there is no fourth place and no sixth.
        That is the assertion worth making: a placement that renumbered
        densely would publish a comparison nobody made, and would look
        entirely plausible.
        """
        clock = MovableClock(NOW)
        tournament, _ = await _seeded_tournament(contract_session, clock, entrants=8, capacity=8)

        await _play_out(contract_session, clock, tournament.id)

        standings = await SqlAlchemyStandingRepository(contract_session).standings_for(
            tournament.id
        )
        assert [s.final_rank for s in standings] == [1, 2, 3, 3, 5, 5, 5, 5]
        assert [s.elimination_round for s in standings] == [None, 3, 2, 2, 1, 1, 1, 1]
        # Every eliminated player names who knocked them out, and nobody
        # eliminates themselves.
        assert all(s.eliminated_by_player_id != s.player_id for s in standings[1:])
        assert all(s.eliminated_by_player_id is not None for s in standings[1:])

    async def test_a_duplicate_completion_returns_the_same_immutable_result(
        self, contract_session: AsyncSession
    ) -> None:
        """§6 — completion is idempotent, and the result does not move.

        A second call must return the **stored** standings rather than
        deriving a second set: identical ranks, identical statistics, no
        duplicated rows and no second champion. The guarantees are the
        database's — `pk_standing`, `uq_standing__one_champion`, and
        `COMPLETED` being terminal.

        Asserted as equality of the whole result, not merely as "does not
        raise": a re-derivation that produced a plausible but different
        placement is exactly what an immutable snapshot exists to prevent.
        """
        clock = MovableClock(NOW)
        tournament, _ = await _seeded_tournament(contract_session, clock, entrants=4, capacity=4)
        await _play_out(contract_session, clock, tournament.id)

        completion = build_completion_service(
            contract_session, events=RecordingPublisher(), clock=MovableClock(NOW + timedelta(1))
        )
        first = await completion.complete_tournament(tournament.id)
        second = await completion.complete_tournament(tournament.id)

        assert first == second
        stored = await SqlAlchemyStandingRepository(contract_session).standings_for(tournament.id)
        assert stored == first
        # The clock moved between the two calls and the snapshot did not.
        assert all(standing.created_at == NOW for standing in stored)


class TestStatistics:
    async def test_draws_and_the_rematch_are_counted_as_games_not_advancements(
        self, contract_session: AsyncSession
    ) -> None:
        """§7 — a drawn game is a draw for both, and the seed tie-break is not
        a win.

        One pairing draws twice, so §6c advances the higher seed by
        adjudication. Both players must show **two draws**, the advancing
        player must show **one adjudicated advancement**, and neither may
        show a win or a loss for that pairing — a fabricated win is a
        competitive fact that never happened, and is unrecoverable once the
        attempts are pruned.
        """
        clock = MovableClock(NOW)
        tournament, _ = await _seeded_tournament(contract_session, clock, entrants=4, capacity=4)
        attempts = SqlAlchemyPairingAttemptRepository(contract_session)
        bracket = SqlAlchemyBracketRepository(contract_session)
        consumer = _consumer(contract_session, clock)

        nodes = await bracket.nodes_for(tournament.id)
        drawn_node = next(n for n in nodes if n.round_number == 1 and n.slot == 0)
        assert drawn_node.id is not None

        first = (await attempts.for_pairings([drawn_node.id]))[0]
        await consumer.handle(
            _completion(match_id=first.match_id, pairing_id=first.pairing_id, winner=None)
        )
        rematch = await attempts.latest_for(drawn_node.id)
        assert rematch is not None and rematch.attempt_number == 2
        await consumer.handle(
            _completion(match_id=rematch.match_id, pairing_id=rematch.pairing_id, winner=None)
        )

        await _play_out(contract_session, clock, tournament.id)

        standings = {
            s.player_id: s
            for s in await SqlAlchemyStandingRepository(contract_session).standings_for(
                tournament.id
            )
        }
        light, dark = first.light_player_id, first.dark_player_id
        assert standings[light].draws == 2
        assert standings[dark].draws == 2

        settled = next(n for n in await bracket.nodes_for(tournament.id) if n.id == drawn_node.id)
        assert settled.advancement_reason is AdvancementReason.ADJUDICATION
        advanced = settled.winner_id
        assert advanced is not None
        assert standings[advanced].adjudicated_advancements == 1
        # The pairing produced no win and no loss for either player.
        eliminated = dark if advanced == light else light
        assert standings[eliminated].wins == 0
        assert standings[eliminated].losses == 0

    async def test_a_bye_creates_no_win_and_no_adjudicated_advancement(
        self, contract_session: AsyncSession
    ) -> None:
        """§7 — a bye moves no counter at all.

        Six entrants in an eight-bracket give the top two seeds a bye. A bye
        is an empty slot (§6a): nobody played, so it is not a win, and
        nothing was decided, so it is not an adjudication either. Counting it
        as the second would be almost right and completely wrong — an
        adjudication is a ruling the platform made, and a bye is arithmetic.
        """
        clock = MovableClock(NOW)
        tournament, _ = await _seeded_tournament(contract_session, clock, entrants=6, capacity=8)
        bracket = SqlAlchemyBracketRepository(contract_session)
        byes = [
            node
            for node in await bracket.nodes_for(tournament.id)
            if node.advancement_reason is AdvancementReason.BYE
        ]
        assert len(byes) == 2
        advanced = {node.winner_id for node in byes}

        await _play_out(contract_session, clock, tournament.id)

        standings = {
            s.player_id: s
            for s in await SqlAlchemyStandingRepository(contract_session).standings_for(
                tournament.id
            )
        }
        for player_id in advanced:
            assert player_id is not None
            assert standings[player_id].adjudicated_advancements == 0
        # Two byes, so the field played five games rather than seven.
        assert sum(s.wins for s in standings.values()) == 5
        assert sum(s.wins for s in standings.values()) == sum(s.losses for s in standings.values())


class TestPublicReads:
    async def test_the_standings_endpoint_is_reachable_and_deterministically_ordered(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§11, §17 — through the **real v1 router**.

        The ordering is `final_rank`, then `seed_number`, then `player_id`,
        and it is asserted as a total order rather than by rank alone: two
        players share third, and an unstable tie-break would page them
        differently between two reads.

        Reached over HTTP so the route, the factory, the read adapter and
        the schema are all proven — a completion invoked only in a unit test
        is exactly the gap §17 names.
        """
        clock = MovableClock(NOW)
        tournament, _ = await _seeded_tournament(contract_session, clock, entrants=8, capacity=8)
        await _play_out(contract_session, clock, tournament.id)
        viewer = await register(client)

        response = await client.get(
            f"/api/v1/tournaments/{tournament.id}/standings", headers=viewer.auth
        )

        assert response.status_code == 200, response.text
        standings = response.json()["data"]["standings"]
        assert [row["final_rank"] for row in standings] == [1, 2, 3, 3, 5, 5, 5, 5]
        assert standings[0]["final_status"] == "champion"
        assert standings[1]["final_status"] == "runner_up"

        expected = await SqlAlchemyTournamentResults(contract_session).standings(tournament.id)
        assert [row["player_id"] for row in standings] == [str(s.player_id) for s in expected]
        assert [(row["final_rank"], row["seed_number"]) for row in standings] == sorted(
            (row["final_rank"], row["seed_number"]) for row in standings
        )

    async def test_the_bracket_endpoint_returns_stable_result_data(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§10 — rounds, nodes, attempts, and nothing internal.

        The attempt summary carries the `match_id`, which is what lets a
        client follow a node to the game that decided it. What must **not**
        appear is the concurrency and policy machinery: a compare-and-set
        target, a no-show deadline or an attendance instant would be
        publishing how the platform works rather than what happened.
        """
        clock = MovableClock(NOW)
        tournament, _ = await _seeded_tournament(contract_session, clock, entrants=4, capacity=4)
        await _play_out(contract_session, clock, tournament.id)
        viewer = await register(client)

        response = await client.get(
            f"/api/v1/tournaments/{tournament.id}/bracket", headers=viewer.auth
        )

        assert response.status_code == 200, response.text
        rounds = response.json()["data"]["rounds"]
        assert [r["round_number"] for r in rounds] == [1, 2]
        assert [len(r["nodes"]) for r in rounds] == [2, 1]

        final = rounds[1]["nodes"][0]
        assert final["winner_id"] is not None
        assert final["advancement_reason"] == "played"
        assert len(final["attempts"]) == 1
        assert final["attempts"][0]["match_id"]
        assert final["attempts"][0]["outcome"] == "decisive"

        # Internals stay internal.
        node_fields = set(final)
        assert not node_fields & {"no_show_deadline", "light_present_at", "dark_present_at"}
        assert not set(final["attempts"][0]) & {"no_show_deadline", "light_present_at"}

    async def test_the_detail_and_history_endpoints_reach_the_composition_root(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§9, §12, §17 — the two remaining routes, over the real router.

        One test for both because they share the thing worth proving: that
        `get_tournament_results` is resolved by a request rather than by a
        test. The detail page shows the lifecycle instants a completed
        tournament has, and the history shows the placing a participant
        earned — which is `null` for a tournament still being played.

        An unknown tournament is `404` and never `403`: §7's rule, and here
        it cannot be got wrong, because a tournament is visible to everybody
        or absent for everybody.
        """
        clock = MovableClock(NOW)
        players = [uuid4() for _ in range(4)]
        tournament, _ = await _seeded_tournament(
            contract_session, clock, entrants=4, capacity=4, players=players
        )
        await _play_out(contract_session, clock, tournament.id)
        viewer = await register(client)

        detail = await client.get(f"/api/v1/tournaments/{tournament.id}", headers=viewer.auth)
        assert detail.status_code == 200, detail.text
        body = detail.json()["data"]
        assert body["status"] == "completed"
        assert body["entrant_count"] == 4
        assert body["completed_at"] is not None
        assert body["current_round"] is None
        assert "created_by" not in body

        history = await client.get(f"/api/v1/players/{players[0]}/tournaments", headers=viewer.auth)
        assert history.status_code == 200, history.text
        entries = history.json()["data"]["entries"]
        assert len(entries) == 1
        assert entries[0]["tournament"]["id"] == str(tournament.id)
        assert entries[0]["final_rank"] in (1, 2, 3)
        assert entries[0]["seed_number"] is not None
        assert history.json()["data"]["next_cursor"] is None

        missing = await client.get(f"/api/v1/tournaments/{uuid4()}", headers=viewer.auth)
        assert missing.status_code == 404
