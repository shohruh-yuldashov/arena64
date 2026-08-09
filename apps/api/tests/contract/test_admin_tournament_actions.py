"""Tournament administration against real PostgreSQL — A64-024.5H.

`tests/unit/test_admin_tournament_actions.py` asserts what the service
decides against a counting fake. This asserts the thing a fake cannot:
that a lifecycle command from `tournament` and an audit entry from `admin`
really do land in **one** transaction.

That claim needs a real database because it is a claim about somebody
else's commit. `TournamentRegistrationService` commits by contract — that
is correct for the operator shell — and the only reason it does not commit
here is that the composition root handed it a `ParticipatingUnitOfWork`.
If that wiring were wrong the unit tests would still pass, because their
fake lifecycle has no transaction to commit.

The rollback case is the one that proves it: a tournament created and an
audit write that fails must leave **no tournament row**. If the inner
service had committed, the row would survive.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.core.clock import SystemClock
from app.core.identifiers import generate_uuid7
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.admin.application.services import AuditRecorder
from app.modules.admin.application.services.tournament_administration_service import (
    TournamentAdministrationService,
)
from app.modules.admin.domain.audit import AuditAction, AuditSubjectType
from app.modules.admin.infrastructure.models import AuditEntryModel
from app.modules.admin.infrastructure.repositories import SqlAlchemyAuditEntryRepository
from app.modules.admin.presentation.dependencies.tournament_actions import (
    TournamentLifecycleAdapter,
)
from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.domain.exceptions import InvalidTournamentTransition
from app.modules.tournament.domain.tournament import TournamentStatus
from app.modules.tournament.infrastructure.models import TournamentModel


@pytest_asyncio.fixture
async def administration(contract_session: AsyncSession) -> TournamentAdministrationService:
    """The **real** graph: `tournament`'s services behind `admin`'s port."""
    return _service(contract_session, entries=SqlAlchemyAuditEntryRepository(contract_session))


def _service(session: AsyncSession, *, entries: object) -> TournamentAdministrationService:
    clock = SystemClock()
    return TournamentAdministrationService(
        lifecycle=TournamentLifecycleAdapter(
            session, settings=get_settings().tournament, clock=clock
        ),
        audit=AuditRecorder(entries=entries, clock=clock),  # type: ignore[arg-type]
        unit_of_work=SessionUnitOfWork(session),
    )


async def _counts(session: AsyncSession) -> tuple[int, int]:
    """Tournaments and audit entries, as fresh statements."""
    return (
        await session.scalar(select(func.count()).select_from(TournamentModel)) or 0,
        await session.scalar(select(func.count()).select_from(AuditEntryModel)) or 0,
    )


class TestTheCommandAndItsAuditAreOneTransaction:
    async def test_creation_writes_the_tournament_and_its_entry_together(
        self,
        administration: TournamentAdministrationService,
        contract_session: AsyncSession,
    ) -> None:
        """Both rows, from one call, through the real services."""
        before = await _counts(contract_session)
        admin = generate_uuid7()

        created = await administration.create(
            name="Friday Blitz",
            variant=ProductVariant.RUSSIAN_8X8,
            speed_class=SpeedClass.BLITZ,
            capacity=8,
            rated=True,
            registration_deadline=None,
            actor_id=admin,
        )

        assert created.status is TournamentStatus.DRAFT
        assert await _counts(contract_session) == (before[0] + 1, before[1] + 1)

        stored = await contract_session.get(TournamentModel, created.tournament_id)
        assert stored is not None
        # §4 — the creator is the administrator the guard resolved, and the
        # id and state are the server's.
        assert stored.created_by == admin
        assert stored.status is TournamentStatus.DRAFT

    async def test_a_failing_audit_leaves_no_tournament(
        self, contract_session: AsyncSession
    ) -> None:
        """**The test this whole design exists for.**

        `TournamentRegistrationService.create` commits by contract. If the
        composition root had not handed it a `ParticipatingUnitOfWork`, the
        tournament would be committed before the audit entry was attempted
        — and this assertion would find a tournament nobody can account
        for.
        """

        class _BrokenEntries:
            async def append(self, entry: object) -> object:
                raise RuntimeError("the audit table is unreachable")

        before = await _counts(contract_session)
        service = _service(contract_session, entries=_BrokenEntries())

        with pytest.raises(RuntimeError, match="unreachable"):
            await service.create(
                name="Never Existed",
                variant=ProductVariant.RUSSIAN_8X8,
                speed_class=SpeedClass.BLITZ,
                capacity=8,
                rated=True,
                registration_deadline=None,
                actor_id=generate_uuid7(),
            )

        assert await _counts(contract_session) == before
        assert (
            await contract_session.scalar(
                select(func.count())
                .select_from(TournamentModel)
                .where(TournamentModel.name == "Never Existed")
            )
            == 0
        )

    async def test_a_transition_and_its_entry_land_together(
        self,
        administration: TournamentAdministrationService,
        contract_session: AsyncSession,
    ) -> None:
        """The whole legal path an administrator can drive, end to end
        through the real aggregate: draft → open → closed.

        `start` is deliberately not driven here — it materialises a bracket
        and creates `game` matches for real entrants, which is
        `tournament`'s own suite's job rather than a duplicate of it.
        """
        admin = generate_uuid7()
        created = await administration.create(
            name="Saturday Rapid",
            variant=ProductVariant.RUSSIAN_8X8,
            speed_class=SpeedClass.BLITZ,
            capacity=8,
            rated=True,
            registration_deadline=None,
            actor_id=admin,
        )

        opened = await administration.open_registration(
            tournament_id=created.tournament_id, actor_id=admin
        )
        assert opened.status is TournamentStatus.REGISTRATION_OPEN

        closed = await administration.close_registration(
            tournament_id=created.tournament_id, actor_id=admin
        )
        assert closed.status is TournamentStatus.REGISTRATION_CLOSED

        stored = await contract_session.get(TournamentModel, created.tournament_id)
        assert stored is not None
        assert stored.status is TournamentStatus.REGISTRATION_CLOSED

        actions = (
            (
                await contract_session.execute(
                    select(AuditEntryModel.action)
                    .where(AuditEntryModel.subject_ref == str(created.tournament_id))
                    .order_by(AuditEntryModel.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert list(actions) == [
            AuditAction.TOURNAMENT_CREATED,
            AuditAction.TOURNAMENT_REGISTRATION_OPENED,
            AuditAction.TOURNAMENT_REGISTRATION_CLOSED,
        ]


class TestTheAggregateStillRefuses:
    async def test_an_illegal_transition_is_refused_and_changes_nothing(
        self,
        administration: TournamentAdministrationService,
        contract_session: AsyncSession,
    ) -> None:
        """`admin` holds no copy of the transition table, so this is the
        aggregate refusing under its own row lock.

        The refusal writes a `FAILED` entry — A64-024.6's policy — and the
        tournament is untouched.
        """
        admin = generate_uuid7()
        created = await administration.create(
            name="Still A Draft",
            variant=ProductVariant.RUSSIAN_8X8,
            speed_class=SpeedClass.BLITZ,
            capacity=8,
            rated=True,
            registration_deadline=None,
            actor_id=admin,
        )

        # `draft` cannot close registration — only `registration_open` can.
        with pytest.raises(InvalidTournamentTransition, match="cannot move from draft"):
            await administration.close_registration(
                tournament_id=created.tournament_id, actor_id=admin
            )

        stored = await contract_session.get(TournamentModel, created.tournament_id)
        assert stored is not None
        assert stored.status is TournamentStatus.DRAFT

        refusals = (
            (
                await contract_session.execute(
                    select(AuditEntryModel).where(
                        AuditEntryModel.subject_ref == str(created.tournament_id),
                        AuditEntryModel.action == AuditAction.TOURNAMENT_TRANSITION_REFUSED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(refusals) == 1
        assert refusals[0].subject_type is AuditSubjectType.TOURNAMENT
        assert refusals[0].after["expected_from"] == TournamentStatus.REGISTRATION_OPEN.value
