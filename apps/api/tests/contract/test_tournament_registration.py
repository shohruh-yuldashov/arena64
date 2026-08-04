"""Registration against real PostgreSQL — A64-019.2 §13.

Against the database rather than a fake, because the two properties that
matter here are the database's: a primary key that refuses a second entry,
and a row lock that stops a concurrent field overflowing. An in-memory
double would model both as `if` statements and prove neither.

Skipped, not failed, when PostgreSQL is unreachable.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.public import ProductVariant
from app.modules.rating.infrastructure.repositories.player_rating_repository import (
    SqlAlchemyRatingReader,
)
from app.modules.rating.public import SpeedClass
from app.modules.tournament.application.ports import (
    AlreadyRegistered,
    NotRegistered,
    NotSeedable,
    PlanAlreadyExists,
    RegistrationNotOpen,
    TournamentIsFull,
)
from app.modules.tournament.application.services.bracket_service import (
    ConflictingWinner,
    TournamentBracketService,
)
from app.modules.tournament.application.services.registration_service import (
    TournamentDeadlineService,
    TournamentRegistrationService,
)
from app.modules.tournament.application.services.seeding_service import (
    TournamentSeedingService,
)
from app.modules.tournament.domain.exceptions import InvalidBracketPosition
from app.modules.tournament.domain.seeding import PlannedPairing
from app.modules.tournament.domain.tournament import TournamentStatus
from app.modules.tournament.infrastructure.rating_snapshots import (
    PublishedRatingSnapshots,
)
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyBracketRepository,
    SqlAlchemyPairingRepository,
    SqlAlchemyRegistrationRepository,
    SqlAlchemySeedRepository,
    SqlAlchemyTournamentRepository,
)
from tests.fakes.outbox import NullUnitOfWork  # noqa: F401 — kept for parity
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import RecordingPublisher

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class _KnownPlayers:
    """A `PlayerDirectory` a test dictates — §3's seam, not `users` itself.

    `users`' own reader is exercised by `users`' suite. What this asserts is
    that `tournament` *asks*, and refuses when the answer is no.
    """

    def __init__(self, *known: UUID) -> None:
        self.known = set(known)

    async def get_profile(self, user_id: UUID) -> object:
        if user_id not in self.known:
            raise LookupError(f"no such player {user_id}")
        return object()


def _service(
    session: AsyncSession, *, players: _KnownPlayers, clock: MovableClock | None = None
) -> TournamentRegistrationService:
    return TournamentRegistrationService(
        tournaments=SqlAlchemyTournamentRepository(session),
        registrations=SqlAlchemyRegistrationRepository(session),
        players=players,
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock or MovableClock(NOW),
    )


async def _open_tournament(
    service: TournamentRegistrationService, *, capacity: int = 4, deadline=None
):  # type: ignore[no-untyped-def]
    tournament = await service.create(
        name="Sunday Open",
        variant=ProductVariant.RUSSIAN_8X8,
        speed_class=SpeedClass.CLASSICAL,
        capacity=capacity,
        registration_deadline=deadline,
    )
    return await service.open_registration(tournament.id)


def _seeding(session: AsyncSession) -> TournamentSeedingService:
    """The seeding service over one session — the composition root's graph,
    assembled here so the test drives the same collaborators."""
    return TournamentSeedingService(
        tournaments=SqlAlchemyTournamentRepository(session),
        seeds=SqlAlchemySeedRepository(session),
        pairings=SqlAlchemyPairingRepository(session),
        ratings=PublishedRatingSnapshots(SqlAlchemyRatingReader(session)),
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=MovableClock(NOW),
    )


def _bracket(session: AsyncSession) -> TournamentBracketService:
    return TournamentBracketService(
        tournaments=SqlAlchemyTournamentRepository(session),
        seeds=SqlAlchemySeedRepository(session),
        pairings=SqlAlchemyPairingRepository(session),
        bracket=SqlAlchemyBracketRepository(session),
        events=RecordingPublisher(),
        unit_of_work=SessionUnitOfWork(session),
        clock=MovableClock(NOW),
    )


class TestRegistering:
    async def test_a_player_enters_once_and_a_second_attempt_is_refused(
        self, contract_session: AsyncSession
    ) -> None:
        """§4 — one registration per player, refused **by the database**.

        The primary key is `(tournament_id, player_id)` with no status
        column, so a second entry is impossible whatever the first one's
        state is. Two concurrent requests could both read nothing and both
        insert; only the constraint stops the second, which is why the
        refusal is asserted rather than a prior read.
        """
        player = uuid4()
        service = _service(contract_session, players=_KnownPlayers(player))
        tournament = await _open_tournament(service)

        registration = await service.register(tournament.id, player)
        assert registration.occupies_a_slot is True
        assert await service.entrant_count(tournament.id) == 1

        with pytest.raises(AlreadyRegistered):
            await service.register(tournament.id, player)

    async def test_an_unknown_player_is_refused_through_the_published_reader(
        self, contract_session: AsyncSession
    ) -> None:
        """§3 — participants are validated through `users.public`.

        `tournament` must not invent its own idea of who exists. The check
        happens **before** the row lock, because it is a read about a player
        rather than about the tournament, and holding a lock across it would
        serialise registrations behind an unrelated query.
        """
        service = _service(contract_session, players=_KnownPlayers())
        tournament = await _open_tournament(service)

        with pytest.raises(LookupError):
            await service.register(tournament.id, uuid4())

        assert await service.entrant_count(tournament.id) == 0

    async def test_registering_after_close_is_refused(self, contract_session: AsyncSession) -> None:
        """§4 — entries are accepted only while registration is open.

        Checked on the **locked** row rather than a value read earlier, so a
        close that lands between the read and the insert cannot let a late
        entry through.
        """
        player = uuid4()
        service = _service(contract_session, players=_KnownPlayers(player))
        tournament = await _open_tournament(service)
        await service.close_registration(tournament.id)

        with pytest.raises(RegistrationNotOpen):
            await service.register(tournament.id, player)


class TestCapacityUnderConcurrency:
    async def test_parallel_registrations_cannot_exceed_capacity(
        self, contract_engine: AsyncEngine
    ) -> None:
        """§6 — the property no unit test can show.

        Six players race for four slots, each on its **own session** so they
        are genuinely concurrent transactions rather than sequential calls.
        Exactly four succeed.

        A unique index cannot do this: it stops one player entering twice
        and says nothing about how many players there are. What does it is
        `SELECT ... FOR UPDATE` on the tournament row before counting — so
        the racers serialise on that row and each sees the ones before it.
        Counting outside the lock is precisely the check-then-insert §6
        forbids, and it passes every test that registers one player at a
        time.
        """
        capacity = 4
        players = [uuid4() for _ in range(6)]
        directory = _KnownPlayers(*players)

        async with AsyncSession(contract_engine) as setup:
            tournament = await _open_tournament(
                _service(setup, players=directory), capacity=capacity
            )
            await setup.commit()

        async def enter(player_id: UUID) -> bool:
            async with AsyncSession(contract_engine) as session:
                try:
                    await _service(session, players=directory).register(tournament.id, player_id)
                except (TournamentIsFull, AlreadyRegistered):
                    return False
                return True

        outcomes = await asyncio.gather(*(enter(player) for player in players))

        assert sum(outcomes) == capacity

        async with AsyncSession(contract_engine) as check:
            assert (
                await _service(check, players=directory).entrant_count(tournament.id)
            ) == capacity


class TestWithdrawal:
    async def test_it_frees_a_slot_before_close_and_is_refused_after(
        self, contract_session: AsyncSession
    ) -> None:
        """§4, §7 — a withdrawal is a status, never a delete.

        The row stays, so "who was in this tournament" is answerable from
        the record; the count drops, so the slot is genuinely free. Both are
        asserted, because a delete would satisfy the second alone.

        After close the field is fixed — the bracket is built from exactly
        those players — so a withdrawal would leave a seat nothing fills.
        It is refused rather than converted to a forfeit: a forfeit is a
        *match* outcome and there is no match yet.
        """
        player = uuid4()
        service = _service(contract_session, players=_KnownPlayers(player))
        tournament = await _open_tournament(service)
        await service.register(tournament.id, player)

        withdrawn = await service.withdraw(tournament.id, player)

        assert withdrawn.occupies_a_slot is False
        assert withdrawn.withdrawn_at == NOW
        assert await service.entrant_count(tournament.id) == 0
        # The row survives — §7's append-oriented record.
        stored = await SqlAlchemyRegistrationRepository(contract_session).find(
            tournament.id, player
        )
        assert stored is not None

        with pytest.raises(NotRegistered):
            await service.withdraw(tournament.id, player)

        await service.close_registration(tournament.id)
        with pytest.raises(RegistrationNotOpen):
            await service.withdraw(tournament.id, uuid4())


class TestTheDeadlineSweep:
    async def test_it_closes_overdue_tournaments_and_is_idempotent(
        self, contract_session: AsyncSession
    ) -> None:
        """§2, §9 — the promise `registration_deadline` makes.

        Registration closes when the deadline is reached without an operator
        being awake. The sweep is **idempotent by predicate**: the claim is
        "open and overdue", so a tournament already closed does not match
        and a second run finds nothing. There is no ledger to keep, which is
        what makes it safe to schedule rather than something to coordinate.

        A tournament whose deadline has *not* passed is left alone, which is
        the half a too-broad predicate would get wrong.
        """
        clock = MovableClock(NOW)
        service = _service(contract_session, players=_KnownPlayers(), clock=clock)

        overdue = await _open_tournament(service, deadline=NOW + timedelta(minutes=5))
        future = await _open_tournament(service, deadline=NOW + timedelta(hours=5))

        sweep = TournamentDeadlineService(
            tournaments=SqlAlchemyTournamentRepository(contract_session),
            registrations=SqlAlchemyRegistrationRepository(contract_session),
            events=RecordingPublisher(),
            unit_of_work=SessionUnitOfWork(contract_session),
            clock=clock,
        )

        assert await sweep.close_overdue() == 0

        clock.advance(600)
        assert await sweep.close_overdue() == 1
        # A second run has nothing to do — the predicate is the guard.
        assert await sweep.close_overdue() == 0

        tournaments = SqlAlchemyTournamentRepository(contract_session)
        assert (await tournaments.by_id(overdue.id)).status is (  # type: ignore[union-attr]
            TournamentStatus.REGISTRATION_CLOSED
        )
        assert (await tournaments.by_id(future.id)).status is (  # type: ignore[union-attr]
            TournamentStatus.REGISTRATION_OPEN
        )


class TestSeeding:
    """A64-019.3 §11, §12 — persistence, idempotency and the race.

    The algorithm is `tests/unit/test_tournament_seeding.py`'s. What only a
    database can show is that a retry returns the *same* plan and that two
    workers cannot write two.
    """

    async def test_seeding_requires_a_closed_registration_and_is_idempotent(
        self, contract_session: AsyncSession
    ) -> None:
        """§2 and §11 in one, because they are the same guarantee twice.

        Seeding an **open** tournament would build a bracket from a field
        that can still change, and the plan is immutable once written — so
        it is refused rather than tolerated, since the failure is otherwise
        invisible: the bracket would simply be missing whoever registered
        next.

        A **second** call returns the persisted plan unchanged. Not merely
        "does not raise": the slots, seats and seeds are compared, because a
        retry that recomputed from current ratings would produce a plausible
        but different bracket, and that is the failure §10 exists to
        prevent.
        """
        players = [uuid4() for _ in range(6)]
        directory = _KnownPlayers(*players)
        service = _service(contract_session, players=directory)
        seeding = _seeding(contract_session)

        tournament = await _open_tournament(service, capacity=8)
        for player in players:
            await service.register(tournament.id, player)

        with pytest.raises(NotSeedable):
            await seeding.seed_tournament(tournament.id)

        await service.close_registration(tournament.id)

        first = await seeding.seed_tournament(tournament.id)
        second = await seeding.seed_tournament(tournament.id)

        assert first == second
        # Six entrants in an eight-bracket: four slots, two of them byes.
        assert len(first) == 4
        assert sum(1 for pairing in first if pairing.is_bye) == 2
        # Seeds are persisted, so no later phase reseeds from live ratings.
        seeds = await SqlAlchemySeedRepository(contract_session).seeds_for(tournament.id)
        assert [seed.seed_number for seed in seeds] == [1, 2, 3, 4, 5, 6]

    async def test_two_workers_cannot_write_two_plans(self, contract_engine: AsyncEngine) -> None:
        """§12 — the race, decided by the primary key.

        Both workers compute a plan; `(tournament, round, slot)` lets
        exactly one insert, and the loser re-reads the winner's. That is
        only safe because seeding is **deterministic**: if the two could
        differ, re-reading would silently accept a bracket the loser did not
        compute. So the assertion is that both callers see the *same* plan,
        not merely that one succeeded.
        """
        players = [uuid4() for _ in range(4)]
        directory = _KnownPlayers(*players)

        async with AsyncSession(contract_engine) as setup:
            service = _service(setup, players=directory)
            tournament = await _open_tournament(service, capacity=4)
            for player in players:
                await service.register(tournament.id, player)
            await service.close_registration(tournament.id)
            await setup.commit()

        async def seed() -> list[object]:
            async with AsyncSession(contract_engine) as session:
                return await _seeding(session).seed_tournament(tournament.id)

        first, second = await asyncio.gather(seed(), seed())

        assert first == second
        async with AsyncSession(contract_engine) as check:
            stored = await SqlAlchemyPairingRepository(check).plan_for(
                tournament.id, round_number=1
            )
        assert len(stored) == 2

    async def test_a_losing_worker_can_read_the_winning_plan_in_its_own_session(
        self, contract_engine: AsyncEngine
    ) -> None:
        """§12's recovery, which the test above never actually reaches.

        `test_two_workers_cannot_write_two_plans` gathers two coroutines on
        one event loop, and they interleave such that the second usually
        finds the persisted plan on its **first read** and returns early —
        so it exercises the idempotent path, not the collision. This forces
        the collision instead: the winner commits, then a second session
        inserts the same plan and must survive to read what the winner
        wrote.

        Without a `SAVEPOINT` around the insert that read raises
        `PendingRollbackError`: a failed statement poisons the enclosing
        transaction, so the loser's recovery fails at precisely the moment
        it exists for. The defect was invisible because it needs a *real*
        collision in a session that is then reused.

        An unrelated constraint still propagates as itself — the savepoint
        scopes the rollback, it does not swallow the error.
        """
        players = [uuid4() for _ in range(4)]
        directory = _KnownPlayers(*players)

        async with AsyncSession(contract_engine) as setup:
            service = _service(setup, players=directory)
            tournament = await _open_tournament(service, capacity=4)
            for player in players:
                await service.register(tournament.id, player)
            await service.close_registration(tournament.id)
            await setup.commit()

        async with AsyncSession(contract_engine) as winner:
            plan = await _seeding(winner).seed_tournament(tournament.id)
            await winner.commit()

        async with AsyncSession(contract_engine) as loser:
            repository = SqlAlchemyPairingRepository(loser)

            with pytest.raises(PlanAlreadyExists):
                await repository.save_plan(tournament.id, plan)

            # The assertion the fix exists for: this session still works.
            assert await repository.plan_for(tournament.id, round_number=1) == plan

            # A different constraint is not a plan collision, and is not
            # translated into one — a round nobody planned, with a player
            # facing themselves.
            with pytest.raises(IntegrityError):
                await repository.save_plan(
                    tournament.id,
                    [
                        PlannedPairing(
                            round_number=9,
                            slot=0,
                            light_player_id=players[0],
                            dark_player_id=players[0],
                            light_seed=1,
                            dark_seed=2,
                        )
                    ],
                )


class TestBracket:
    """A64-019.4 §5, §7, §8 — materialisation, advancement and the race."""

    async def test_the_whole_tree_is_written_once_and_a_retry_returns_it(
        self, contract_session: AsyncSession
    ) -> None:
        """§1, §5 — built whole, not lazily, and idempotent.

        Later rounds are materialised **empty** rather than created when the
        previous one finishes: a bracket generated from current results can
        differ from the one players read, and "who could I meet in the
        semi-final" stops being answerable in advance.

        The retry is compared node by node, not merely for absence of an
        error — a second materialisation that recomputed placement would
        produce a plausible but different tree, which is what §1's
        immutability exists to prevent.
        """
        players = [uuid4() for _ in range(4)]
        directory = _KnownPlayers(*players)
        service = _service(contract_session, players=directory)
        tournament = await _open_tournament(service, capacity=4)
        for player in players:
            await service.register(tournament.id, player)
        await service.close_registration(tournament.id)
        await _seeding(contract_session).seed_tournament(tournament.id)

        bracket = _bracket(contract_session)
        first = await bracket.materialise(tournament.id)
        second = await bracket.materialise(tournament.id)

        assert first == second
        assert len(first) == 3  # two semi-finals and a final
        assert {(n.round_number, n.slot) for n in first} == {(1, 0), (1, 1), (2, 0)}
        # The final is materialised empty and waiting.
        final = next(n for n in first if n.round_number == 2)
        assert final.participants == ()

    async def test_advancing_a_winner_fills_the_parent_and_repeats_harmlessly(
        self, contract_session: AsyncSession
    ) -> None:
        """§7 — the parent seat, filled exactly once.

        Even slots feed the light seat and odd ones the dark, so a retry
        lands in the same seat rather than a second one. The repeat is
        asserted to leave the bracket **identical**, not merely to avoid
        raising: an advancement that filled the other seat would produce a
        final where one player appears twice.

        A winner who did not play in the node is refused — the one bracket
        error nothing downstream detects.
        """
        players = [uuid4() for _ in range(4)]
        directory = _KnownPlayers(*players)
        service = _service(contract_session, players=directory)
        tournament = await _open_tournament(service, capacity=4)
        for player in players:
            await service.register(tournament.id, player)
        await service.close_registration(tournament.id)
        await _seeding(contract_session).seed_tournament(tournament.id)

        bracket = _bracket(contract_session)
        nodes = await bracket.materialise(tournament.id)
        semi = next(n for n in nodes if n.round_number == 1 and n.slot == 0)
        winner = semi.participants[0]

        advanced = await bracket.advance_winner(
            tournament.id, round_number=1, slot=0, winner_id=winner
        )
        final = next(n for n in advanced if n.round_number == 2)
        assert final.light_player_id == winner
        assert final.light_seed is not None

        repeated = await bracket.advance_winner(
            tournament.id, round_number=1, slot=0, winner_id=winner
        )
        assert repeated == advanced

        with pytest.raises(InvalidBracketPosition):
            await bracket.advance_winner(tournament.id, round_number=1, slot=0, winner_id=uuid4())

    async def test_two_workers_cannot_advance_two_different_winners(
        self, contract_engine: AsyncEngine
    ) -> None:
        """§8 — the compare-and-set, under real concurrency.

        Two workers process the same node with **different** winners. The
        `UPDATE … WHERE winner_id IS NULL` lets exactly one write; the other
        finds a stored winner it disagrees with and raises rather than
        overwriting.

        Overwriting is the failure that matters: on a bracket it means a
        player advancing out of a node they lost, and by the time it is
        visible the rounds above have been recorded. Read-then-write would
        let both through.
        """
        players = [uuid4() for _ in range(4)]
        directory = _KnownPlayers(*players)

        async with AsyncSession(contract_engine) as setup:
            service = _service(setup, players=directory)
            tournament = await _open_tournament(service, capacity=4)
            for player in players:
                await service.register(tournament.id, player)
            await service.close_registration(tournament.id)
            await _seeding(setup).seed_tournament(tournament.id)
            nodes = await _bracket(setup).materialise(tournament.id)
            await setup.commit()

        semi = next(n for n in nodes if n.round_number == 1 and n.slot == 0)
        light, dark = semi.light_player_id, semi.dark_player_id

        async def advance(winner: UUID) -> str:
            async with AsyncSession(contract_engine) as session:
                try:
                    await _bracket(session).advance_winner(
                        tournament.id, round_number=1, slot=0, winner_id=winner
                    )
                except ConflictingWinner:
                    return "conflict"
                return "won"

        outcomes = await asyncio.gather(advance(light), advance(dark))

        assert sorted(outcomes) == ["conflict", "won"]

        async with AsyncSession(contract_engine) as check:
            stored = await SqlAlchemyBracketRepository(check).nodes_for(tournament.id)
        decided = next(n for n in stored if (n.round_number, n.slot) == (1, 0))
        assert decided.winner_id in (light, dark)
        final = next(n for n in stored if n.round_number == 2)
        # Exactly one player advanced — not both.
        assert len(final.participants) == 1
