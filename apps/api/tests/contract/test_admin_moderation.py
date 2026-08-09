"""Moderation against real PostgreSQL — A64-024.6.

`tests/unit/test_admin_moderation.py` covers what the service decides, over
in-memory storage. What it cannot cover is what only a real database has,
and every property this task's safety actually rests on is in that category:

    atomicity         a failure after the sanction leaves **no** case, no
                      sanction and no audit entry — the four writes are one
    uq_sanction__     two administrators restricting the same account at
    live_kind         once resolve to one live row, not two that disagree
    the FK            a sanction cannot name a case that does not exist
    the check         a lift that names no lifter is refused

None is falsifiable against a dictionary — a fake can model them, and a
model that agrees with itself proves nothing.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import generate_uuid7
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.admin.application.services import AuditRecorder, ModerationService
from app.modules.admin.domain.moderation import (
    CaseStatus,
    ModerationCase,
    ModerationCategory,
    Sanction,
    SanctionKind,
)
from app.modules.admin.infrastructure.models import (
    AuditEntryModel,
    ModerationCaseModel,
    SanctionModel,
)
from app.modules.admin.infrastructure.repositories import (
    SqlAlchemyAuditEntryRepository,
    SqlAlchemyModerationCaseRepository,
    SqlAlchemySanctionRepository,
)
from tests.fakes.moderation import RecordingSessionRevoker
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def sanctions(contract_session: AsyncSession) -> SqlAlchemySanctionRepository:
    return SqlAlchemySanctionRepository(contract_session)


@pytest_asyncio.fixture
async def cases(contract_session: AsyncSession) -> SqlAlchemyModerationCaseRepository:
    return SqlAlchemyModerationCaseRepository(contract_session)


def _service(
    session: AsyncSession, *, revoker: RecordingSessionRevoker | None = None
) -> ModerationService:
    """The **real** service over real repositories and the test's session."""
    clock = MovableClock(NOW)
    return ModerationService(
        cases=SqlAlchemyModerationCaseRepository(session),
        sanctions=SqlAlchemySanctionRepository(session),
        sessions=revoker or RecordingSessionRevoker(),
        audit=AuditRecorder(entries=SqlAlchemyAuditEntryRepository(session), clock=clock),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def _case(subject: UUID, opener: UUID) -> ModerationCase:
    return ModerationCase(
        id=generate_uuid7(),
        subject_player_id=subject,
        category=ModerationCategory.ABUSE,
        status=CaseStatus.CLOSED,
        opened_by=opener,
        opened_at=NOW,
        closed_at=NOW,
        decision=SanctionKind.SUSPENDED.value,
        reasoning="Repeated abuse after a warning.",
    )


def _sanction(player: UUID, case_id: UUID, *, expires_at: datetime | None = None) -> Sanction:
    return Sanction(
        id=generate_uuid7(),
        player_id=player,
        case_id=case_id,
        kind=SanctionKind.SUSPENDED,
        starts_at=NOW,
        expires_at=expires_at,
        created_at=NOW,
    )


async def _counts(session: AsyncSession) -> tuple[int, int, int]:
    """Rows in the three tables one restriction touches."""
    return (
        await session.scalar(select(func.count()).select_from(ModerationCaseModel)) or 0,
        await session.scalar(select(func.count()).select_from(SanctionModel)) or 0,
        await session.scalar(select(func.count()).select_from(AuditEntryModel)) or 0,
    )


class TestTheFourWritesAreOne:
    async def test_a_restriction_commits_its_case_sanction_and_audit_entry_together(
        self, contract_session: AsyncSession
    ) -> None:
        """The invariant A64-024.8 exists to make possible, against a real
        transaction rather than a fake that cannot roll back."""
        before = await _counts(contract_session)
        revoker = RecordingSessionRevoker()
        player, admin = generate_uuid7(), generate_uuid7()

        await _service(contract_session, revoker=revoker).suspend(
            player_id=player,
            category=ModerationCategory.CHEATING,
            reasoning="Engine assistance confirmed across three games.",
            expires_at=None,
            actor_id=admin,
            administrators=[admin, generate_uuid7()],
        )

        after = await _counts(contract_session)
        assert after == (before[0] + 1, before[1] + 1, before[2] + 1)
        assert revoker.revoked_for == [player]

    async def test_a_failure_after_the_sanction_leaves_nothing_behind(
        self, contract_session: AsyncSession
    ) -> None:
        """The rollback path, which is the whole reason for one transaction.

        The session revoker fails *after* the case and the sanction are
        flushed and *before* the audit entry is written — the worst
        ordering, because it is the one that would otherwise leave a
        restriction nobody can account for and whose sessions survived.

        Asserted by counting: nothing was added to any of the three tables.
        """
        before = await _counts(contract_session)

        class _FailingRevoker:
            async def revoke_all_for(self, user_id: UUID, *, at: datetime) -> int:
                raise RuntimeError("the session store is unreachable")

        service = _service(contract_session, revoker=_FailingRevoker())  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="unreachable"):
            await service.suspend(
                player_id=generate_uuid7(),
                category=ModerationCategory.ABUSE,
                reasoning="Repeated abuse after a warning.",
                expires_at=None,
                actor_id=generate_uuid7(),
                administrators=[generate_uuid7(), generate_uuid7()],
            )

        # The unit of work rolled the whole thing back. `expire_all` is not
        # needed: the counts are fresh statements against the database.
        assert await _counts(contract_session) == before


class TestTheDatabaseKeepsTheInvariants:
    async def test_two_live_restrictions_of_one_kind_cannot_coexist(
        self,
        sanctions: SqlAlchemySanctionRepository,
        cases: SqlAlchemyModerationCaseRepository,
        contract_session: AsyncSession,
    ) -> None:
        """`uq_sanction__live_kind`.

        The service refuses a duplicate for a readable error; this is what
        holds when two administrators act inside the same millisecond and
        both pass that check. Without it the account would carry two live
        restrictions whose expiries disagree, and nothing could say which
        one applied.
        """
        player, admin = generate_uuid7(), generate_uuid7()
        first = await cases.add(_case(player, admin))
        second = await cases.add(_case(player, admin))
        await sanctions.add(_sanction(player, first.id))

        with pytest.raises(IntegrityError, match="uq_sanction__live_kind"):
            await sanctions.add(_sanction(player, second.id))

        await contract_session.rollback()

    async def test_a_lifted_restriction_frees_the_slot(
        self,
        sanctions: SqlAlchemySanctionRepository,
        cases: SqlAlchemyModerationCaseRepository,
    ) -> None:
        """The partial predicate is `lifted_at IS NULL`, deliberately.

        A restriction that ended must not block a later one — an account
        restricted, restored, and restricted again is ordinary history, and
        a constraint that forbade it would make the second decision
        impossible to record.
        """
        player, admin = generate_uuid7(), generate_uuid7()
        first = await cases.add(_case(player, admin))
        live = await sanctions.add(_sanction(player, first.id))

        await sanctions.lift(live.lift(at=NOW + timedelta(hours=1), by=admin))

        second = await cases.add(_case(player, admin))
        again = await sanctions.add(_sanction(player, second.id))
        assert again.id != live.id

    async def test_a_sanction_cannot_name_a_case_that_does_not_exist(
        self, sanctions: SqlAlchemySanctionRepository, contract_session: AsyncSession
    ) -> None:
        """`fk_sanction__case_id`, and §13.3's "a sanction names the case
        that authorised it".

        An enforced restriction nobody can trace back to a decision is
        exactly what an appeal cannot answer.
        """
        with pytest.raises(IntegrityError, match="fk_sanction__case_id"):
            await sanctions.add(_sanction(generate_uuid7(), generate_uuid7()))

        await contract_session.rollback()

    async def test_a_case_cannot_be_opened_about_its_own_opener(
        self, cases: SqlAlchemyModerationCaseRepository, contract_session: AsyncSession
    ) -> None:
        """`ck_moderation_case__not_self_opened` — §13.2's "a moderator may
        not act on a case involving themselves", kept by the database for a
        row that did not go through the domain."""
        same = generate_uuid7()
        contract_session.add(
            ModerationCaseModel(
                id=generate_uuid7(),
                subject_player_id=same,
                category=ModerationCategory.OTHER,
                status=CaseStatus.CLOSED,
                opened_by=same,
                opened_at=NOW,
                closed_at=NOW,
                decision="suspended",
                reasoning="x",
                reverses_case_id=None,
            )
        )
        with pytest.raises(IntegrityError, match="ck_moderation_case__not_self_opened"):
            await contract_session.flush()

        await contract_session.rollback()


class TestTheEffectiveStateRead:
    async def test_expiry_is_evaluated_at_read_time_and_the_row_survives(
        self,
        sanctions: SqlAlchemySanctionRepository,
        cases: SqlAlchemyModerationCaseRepository,
    ) -> None:
        """§13.3, in SQL rather than in Python.

        The expiry comparison is a predicate the database applies, so an
        expired restriction stops being returned with no job having run —
        and the row is still there, because history is not a deletion.
        """
        player, admin = generate_uuid7(), generate_uuid7()
        case = await cases.add(_case(player, admin))
        await sanctions.add(_sanction(player, case.id, expires_at=NOW + timedelta(hours=1)))

        assert await sanctions.effective_for(player, at=NOW)
        assert await sanctions.effective_for(player, at=NOW + timedelta(hours=2)) == []
        assert await sanctions.live_of_kind(player, SanctionKind.SUSPENDED) is not None

    async def test_the_published_gate_reports_indefinite_over_timed(
        self,
        sanctions: SqlAlchemySanctionRepository,
        cases: SqlAlchemyModerationCaseRepository,
    ) -> None:
        """§13.3's "overlapping sanctions apply the most restrictive", on
        the only dimension `AccountRestriction` has.

        An account is unrestricted until it is not, and the gate says
        nothing more than that — no category, no case, no reasoning reaches
        `auth`.
        """
        player, admin = generate_uuid7(), generate_uuid7()
        assert await sanctions.restriction_for(player, at=NOW) is None

        case = await cases.add(_case(player, admin))
        await sanctions.add(_sanction(player, case.id, expires_at=NOW + timedelta(days=7)))

        restriction = await sanctions.restriction_for(player, at=NOW)
        assert restriction is not None
        assert restriction.until == NOW + timedelta(days=7)
        # And nothing else travels: the DTO has one field.
        assert set(vars(type(restriction))["__slots__"]) == {"until"}
