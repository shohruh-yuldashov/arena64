"""The `Depends` bridge for `admin` — dependency-injection.md DI-01.

`Depends` at the routing layer only, handing an already-resolved service to
a guard. Not a container.

The service is returned rather than the repository, because granting and
revoking carry rules — self-grant, last-administrator, the attribution of a
granter — that a route holding a repository could bypass by writing a row.

`AuditLog` is returned for the same reason in reverse: it is the **reader**
of the trail and has no write, so a route holding it cannot append to the
record of what administrators have done. Appending is `AuditRecorder`'s, and
that is deliberately not wired to any route — an entry is written by the
service performing the action, never by a request asking for one.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.admin.application.services import AdminRoleService, AuditLog, AuditRecorder
from app.modules.admin.infrastructure.repositories import (
    SqlAlchemyAuditEntryRepository,
    SqlAlchemyRoleAssignmentRepository,
)


def get_admin_role_service(session: DbSessionDep, clock: ClockDep) -> AdminRoleService:
    """The per-request role service, over the request's session.

    The recorder shares that session, which is what makes a grant and its
    audit entry one transaction rather than two.
    """
    return AdminRoleService(
        assignments=SqlAlchemyRoleAssignmentRepository(session),
        audit=AuditRecorder(entries=SqlAlchemyAuditEntryRepository(session), clock=clock),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def get_audit_log(session: DbSessionDep) -> AuditLog:
    """The per-request audit reader. **No write reachable from it.**"""
    return AuditLog(entries=SqlAlchemyAuditEntryRepository(session))


AdminRoleServiceDep = Annotated[AdminRoleService, Depends(get_admin_role_service)]
AuditLogDep = Annotated[AuditLog, Depends(get_audit_log)]

__all__ = ["AdminRoleServiceDep", "AuditLogDep", "get_admin_role_service", "get_audit_log"]
