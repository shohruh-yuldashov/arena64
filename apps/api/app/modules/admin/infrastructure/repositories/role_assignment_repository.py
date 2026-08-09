"""`RoleAssignmentRepository` over SQLAlchemy — repositories.md §3.

Maps rows to `RoleAssignment` values and back. Nothing above this file sees
a `RoleAssignmentModel`, which is what lets the guard hold a domain value
and no ORM identity.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.domain.roles import AdminRole, RoleAssignment
from app.modules.admin.infrastructure.models import RoleAssignmentModel


def _to_domain(row: RoleAssignmentModel) -> RoleAssignment:
    return RoleAssignment(
        id=row.id,
        account_id=row.account_id,
        role=row.role,
        granted_by=row.granted_by,
        granted_at=row.granted_at,
        revoked_at=row.revoked_at,
    )


class SqlAlchemyRoleAssignmentRepository:
    """Grants, in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def live_roles_for(self, account_id: UUID) -> frozenset[AdminRole]:
        """The authorization read — one indexed lookup on `account_id`.

        Selects the **role column alone** rather than whole rows: the guard
        needs membership, and loading grant metadata it will not read would
        be work on every admin request.
        """
        rows = await self._session.execute(
            select(RoleAssignmentModel.role).where(
                RoleAssignmentModel.account_id == account_id,
                RoleAssignmentModel.revoked_at.is_(None),
            )
        )
        return frozenset(rows.scalars().all())

    async def live_for(self, account_id: UUID, role: AdminRole) -> RoleAssignment | None:
        row = await self._session.scalar(
            select(RoleAssignmentModel).where(
                RoleAssignmentModel.account_id == account_id,
                RoleAssignmentModel.role == role,
                RoleAssignmentModel.revoked_at.is_(None),
            )
        )
        return None if row is None else _to_domain(row)

    async def add(self, assignment: RoleAssignment) -> RoleAssignment:
        self._session.add(
            RoleAssignmentModel(
                id=assignment.id,
                account_id=assignment.account_id,
                role=assignment.role,
                granted_by=assignment.granted_by,
                granted_at=assignment.granted_at,
                revoked_at=assignment.revoked_at,
            )
        )
        # Flushed rather than committed: the unit of work owns the
        # transaction boundary (repositories.md §5.1), and flushing here is
        # what surfaces `uq_role_assignment__live` as an error the service
        # can attribute rather than one that appears at commit.
        await self._session.flush()
        return assignment

    async def revoke(self, assignment: RoleAssignment) -> RoleAssignment:
        row = await self._session.get(RoleAssignmentModel, assignment.id)
        if row is None:  # pragma: no cover — the service read it moments ago
            return assignment
        row.revoked_at = assignment.revoked_at
        await self._session.flush()
        return assignment

    async def live_holders_of(self, role: AdminRole) -> Sequence[UUID]:
        rows = await self._session.execute(
            select(RoleAssignmentModel.account_id).where(
                RoleAssignmentModel.role == role,
                RoleAssignmentModel.revoked_at.is_(None),
            )
        )
        return list(rows.scalars().all())


__all__ = ["SqlAlchemyRoleAssignmentRepository"]
