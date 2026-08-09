"""The `Depends` bridge for `admin` — dependency-injection.md DI-01.

`Depends` at the routing layer only, handing an already-resolved service to
a guard. Not a container.

The service is returned rather than the repository, because granting and
revoking carry rules — self-grant, last-administrator, the attribution of a
granter — that a route holding a repository could bypass by writing a row.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.admin.application.services import AdminRoleService
from app.modules.admin.infrastructure.repositories import SqlAlchemyRoleAssignmentRepository


def get_admin_role_service(session: DbSessionDep, clock: ClockDep) -> AdminRoleService:
    """The per-request role service, over the request's session."""
    return AdminRoleService(
        assignments=SqlAlchemyRoleAssignmentRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


AdminRoleServiceDep = Annotated[AdminRoleService, Depends(get_admin_role_service)]

__all__ = ["AdminRoleServiceDep", "get_admin_role_service"]
