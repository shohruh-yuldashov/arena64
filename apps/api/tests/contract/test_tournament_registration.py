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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.application.ports import (
    AlreadyRegistered,
    NotRegistered,
    RegistrationNotOpen,
    TournamentIsFull,
)
from app.modules.tournament.application.services.registration_service import (
    TournamentDeadlineService,
    TournamentRegistrationService,
)
from app.modules.tournament.domain.tournament import TournamentStatus
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyRegistrationRepository,
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
