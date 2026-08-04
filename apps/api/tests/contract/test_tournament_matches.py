"""Live tournament matches, against real PostgreSQL — A64-019.5 §12.

Three tests, and each asserts something no unit test can: that a `game`
match really was created with the reference that finds it again, that a
redelivered completion changes nothing, and that a drawn pairing produces
exactly one rematch and then stops.

Driven through the **real** `game` command port (`build_match_creation`) and
the **real** outbox consumer, so the round trip A64-019.0 opened and this
phase closes — `origin_ref` out, `origin_ref` back — is exercised end to end
rather than modelled. A fake match creator would prove the tournament calls
something; it would not prove `game` stores the reference or hands it back.

What is deliberately **not** re-tested here: `game`'s match lifecycle, the
rating arithmetic, and the draw policy itself. The first two have their own
suites and the third is `tests/unit/test_tournament_attempts.py`'s — what
this file adds is the wiring between them.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.domain.events import MatchCompleted
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import MatchOrigin
from app.modules.game.infrastructure.models import MatchRecordModel
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
from app.modules.tournament.domain.attempts import (
    AdvancementReason,
    AttemptOutcome,
    AttemptStatus,
)
from app.modules.tournament.infrastructure.rating_snapshots import (
    PublishedRatingSnapshots,
)
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyBracketRepository,
    SqlAlchemyPairingAttemptRepository,
    SqlAlchemyPairingRepository,
    SqlAlchemyRegistrationRepository,
    SqlAlchemyRoundRepository,
    SqlAlchemySeedRepository,
    SqlAlchemyTournamentRepository,
)
from app.modules.tournament.presentation.dependencies import (
    build_advancement_service,
)
from app.platform.outbox import OutboxEntry
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import RecordingPublisher

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class _KnownPlayers:
    """A `PlayerDirectory` a test dictates — the same seam
    `test_tournament_registration.py` uses, for the same reason."""

    def __init__(self, *known: UUID) -> None:
        self.known = set(known)

    async def get_profile(self, user_id: UUID) -> object:
        if user_id not in self.known:
            raise LookupError(f"no such player {user_id}")
        return object()


def _registration(session: AsyncSession, players: _KnownPlayers, clock: MovableClock):  # type: ignore[no-untyped-def]
    return TournamentRegistrationService(
        tournaments=SqlAlchemyTournamentRepository(session),
        registrations=SqlAlchemyRegistrationRepository(session),
        players=players,
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def _launcher(session: AsyncSession, clock: MovableClock) -> TournamentMatchLauncher:
    """The real `game` command port, over the same session.

    `build_match_creation` is `matchmaking`'s composition factory for
    `game`'s `PersistentMatchCreation` — the object `app_factory` hands the
    tournament launcher. Using it here is what makes this suite prove the
    edge rather than a stand-in for it.
    """
    return TournamentMatchLauncher(
        matches=build_match_creation(session, events=RecordingPublisher(), clock=clock),
        ratings=PublishedRatingSnapshots(SqlAlchemyRatingReader(session)),
        attempts=SqlAlchemyPairingAttemptRepository(session),
        clock=clock,
    )


def _bracket(session: AsyncSession, clock: MovableClock) -> TournamentBracketService:
    return TournamentBracketService(
        tournaments=SqlAlchemyTournamentRepository(session),
        seeds=SqlAlchemySeedRepository(session),
        pairings=SqlAlchemyPairingRepository(session),
        bracket=SqlAlchemyBracketRepository(session),
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def _start(session: AsyncSession, clock: MovableClock) -> TournamentStartService:
    return TournamentStartService(
        tournaments=SqlAlchemyTournamentRepository(session),
        brackets=_bracket(session, clock),
        bracket=SqlAlchemyBracketRepository(session),
        rounds=SqlAlchemyRoundRepository(session),
        attempts=SqlAlchemyPairingAttemptRepository(session),
        launcher=_launcher(session, clock),
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def _consumer(session: AsyncSession, clock: MovableClock) -> TournamentMatchCompletionConsumer:
    """The production consumer graph, minus only the `game` factory's
    publisher — so a test drives exactly what the relay drives."""
    return TournamentMatchCompletionConsumer(
        build_advancement_service(
            session,
            matches=build_match_creation(session, events=RecordingPublisher(), clock=clock),
            events=RecordingPublisher(),
            clock=clock,
        )
    )


async def _running_tournament(
    session: AsyncSession, clock: MovableClock, *, entrants: int, capacity: int
):  # type: ignore[no-untyped-def]
    """A tournament seeded, bracketed and started. Returns it and its attempts."""
    players = [uuid4() for _ in range(entrants)]
    directory = _KnownPlayers(*players)
    registration = _registration(session, directory, clock)

    tournament = await registration.create(
        name="Sunday Open",
        variant=ProductVariant.RUSSIAN_8X8,
        speed_class=SpeedClass.CLASSICAL,
        capacity=capacity,
    )
    tournament = await registration.open_registration(tournament.id)
    for player in players:
        await registration.register(tournament.id, player)
    await registration.close_registration(tournament.id)

    seeding = TournamentSeedingService(
        tournaments=SqlAlchemyTournamentRepository(session),
        seeds=SqlAlchemySeedRepository(session),
        pairings=SqlAlchemyPairingRepository(session),
        ratings=PublishedRatingSnapshots(SqlAlchemyRatingReader(session)),
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    await seeding.seed_tournament(tournament.id)

    attempts = await _start(session, clock).start_tournament(tournament.id)
    return tournament, attempts


def _completion(
    *, match_id: UUID, pairing_id: UUID, winner: PlayerSide | None
) -> list[OutboxEntry]:
    """One `game.match_completed` entry, built from the real event.

    `MatchCompleted.payload()` rather than a hand-written dict, so a field
    renamed in `game` breaks this test instead of silently making the
    consumer skip every entry — which is exactly how a consumer goes
    quietly dead.
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
        ply_number=42,
        origin=MatchOrigin.TOURNAMENT,
        origin_ref=pairing_id,
    )
    return [OutboxEntry.of(event)]


class TestStartingATournament:
    async def test_it_creates_a_match_for_every_real_pairing_and_none_for_a_bye(
        self, contract_session: AsyncSession
    ) -> None:
        """§5, §6a — a bye is an empty slot, never a match.

        Six entrants in an eight-bracket: four round-one nodes, two of them
        byes. Two matches, not four, and the byes are already decided
        without one — asserted together, because a launcher that created
        four would still leave the bracket "correct" until two players were
        told to play somebody who is not there.

        The `origin_ref` assertion is the whole of A64-019.0 becoming true:
        `game` stores the node's own id, which is how the completion finds
        its way back. A match with the right players and no reference is a
        game nobody can advance from.
        """
        clock = MovableClock(NOW)
        tournament, attempts = await _running_tournament(
            contract_session, clock, entrants=6, capacity=8
        )

        assert len(attempts) == 2

        nodes = await SqlAlchemyBracketRepository(contract_session).nodes_for(tournament.id)
        first_round = [node for node in nodes if node.round_number == 1]
        byes = [node for node in first_round if node.advancement_reason is AdvancementReason.BYE]
        assert len(byes) == 2
        assert all(node.winner_id is not None for node in byes)

        launched = {attempt.pairing_id for attempt in attempts}
        assert launched == {node.id for node in first_round if node.needs_a_match}
        assert not launched & {node.id for node in byes}

        # The round trip, read from `game`'s own table.
        for attempt in attempts:
            record = await contract_session.scalar(
                select(MatchRecordModel).where(MatchRecordModel.id == attempt.match_id)
            )
            assert record is not None
            assert record.origin is MatchOrigin.TOURNAMENT
            assert record.origin_ref == attempt.pairing_id

        # Starting again launches nothing new — `game`'s derived key and
        # `unique (pairing_id, attempt_number)` both refuse a second match.
        repeated = await _start(contract_session, clock).start_tournament(tournament.id)
        assert {a.match_id for a in repeated} == {a.match_id for a in attempts}
        assert await _match_count(contract_session, MatchOrigin.TOURNAMENT) == 2


class TestAdvancingOnCompletion:
    async def test_a_decisive_result_advances_the_winner_and_redelivery_changes_nothing(
        self, contract_session: AsyncSession
    ) -> None:
        """§6c, §8 — the consumer's whole job, and its idempotency.

        The redelivery is asserted to leave the bracket **identical**, not
        merely to avoid raising: at-least-once is the outbox's contract, so
        a second delivery is ordinary rather than exceptional, and an
        advancement applied twice would fill the final's other seat with the
        same player.

        Driven through `MatchCompleted.payload()` and the real consumer, so
        the decode is exercised too — a consumer that skipped every entry
        because a field was renamed would pass a test that called the
        service directly.
        """
        clock = MovableClock(NOW)
        tournament, attempts = await _running_tournament(
            contract_session, clock, entrants=4, capacity=4
        )
        assert len(attempts) == 2

        played = attempts[0]
        consumer = _consumer(contract_session, clock)
        entries = _completion(
            match_id=played.match_id, pairing_id=played.pairing_id, winner=PlayerSide.LIGHT
        )

        assert await consumer.handle(entries) == []

        bracket = SqlAlchemyBracketRepository(contract_session)
        after = {(n.round_number, n.slot): n for n in await bracket.nodes_for(tournament.id)}
        decided = next(n for n in after.values() if n.id == played.pairing_id)
        assert decided.winner_id == played.light_player_id
        assert decided.advancement_reason is AdvancementReason.PLAYED

        final = after[(2, 0)]
        assert played.light_player_id in final.participants
        assert len(final.participants) == 1  # the other semi has not been played

        stored = await SqlAlchemyPairingAttemptRepository(contract_session).by_match(
            played.match_id
        )
        assert stored is not None
        assert stored.status is AttemptStatus.COMPLETED
        assert stored.outcome is AttemptOutcome.DECISIVE

        assert await consumer.handle(entries) == []
        assert {
            (n.round_number, n.slot): n for n in await bracket.nodes_for(tournament.id)
        } == after


class TestDrawsAndTheBoundedRematch:
    async def test_a_draw_rematches_with_swapped_seats_and_a_second_draw_adjudicates(
        self, contract_session: AsyncSession
    ) -> None:
        """§6c — the whole policy, in the order it actually happens.

        The first draw produces **exactly one** rematch with the seats
        swapped: repeating them would give one player the first move in both
        games of a tie. The second draw produces **no third match** — the
        higher seed advances by adjudication instead, which is the bound
        that stops a tournament that can never finish.

        The adjudication is asserted to be recorded as such rather than as
        an ordinary win, because the distinction is a competitive fact
        somebody will ask about and it is unrecoverable once the attempts
        are pruned.
        """
        clock = MovableClock(NOW)
        tournament, attempts = await _running_tournament(
            contract_session, clock, entrants=4, capacity=4
        )
        first = attempts[0]
        consumer = _consumer(contract_session, clock)
        repository = SqlAlchemyPairingAttemptRepository(contract_session)

        await consumer.handle(
            _completion(match_id=first.match_id, pairing_id=first.pairing_id, winner=None)
        )

        rematch = await repository.latest_for(first.pairing_id)
        assert rematch is not None
        assert rematch.attempt_number == 2
        assert rematch.match_id != first.match_id
        # Swapped, which is the point of a rematch's seats.
        assert (rematch.light_player_id, rematch.dark_player_id) == (
            first.dark_player_id,
            first.light_player_id,
        )

        bracket = SqlAlchemyBracketRepository(contract_session)
        undecided = next(
            n for n in await bracket.nodes_for(tournament.id) if n.id == first.pairing_id
        )
        assert undecided.winner_id is None  # a draw advances nobody

        await consumer.handle(
            _completion(match_id=rematch.match_id, pairing_id=rematch.pairing_id, winner=None)
        )

        node = next(n for n in await bracket.nodes_for(tournament.id) if n.id == first.pairing_id)
        assert node.advancement_reason is AdvancementReason.ADJUDICATION
        assert node.winner_id == _higher_seed(node)

        # **No third match.** The bound is the point — two attempts, ever.
        assert len(await repository.for_pairings([first.pairing_id])) == 2


async def _match_count(session: AsyncSession, origin: MatchOrigin) -> int:
    rows = await session.scalars(
        select(MatchRecordModel.id).where(MatchRecordModel.origin == origin)
    )
    return len(list(rows))


def _higher_seed(node) -> UUID:  # type: ignore[no-untyped-def]
    """The better-seeded participant — seed numbers count up from the best."""
    if node.light_seed is None or node.dark_seed is None:
        pytest.fail("a played node has a seed on both seats")
    return node.light_player_id if node.light_seed < node.dark_seed else node.dark_player_id
