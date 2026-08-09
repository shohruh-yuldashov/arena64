"""Notification operations, wired — dependency-injection.md DI-01. A64-024.7.

Two dependencies over one adapter, and the split is the point: a route that
reads deliveries holds the **directory**, and only the retry route holds the
**service**. The directory's type has no mutation on it, so the two read
routes could not re-arm a delivery even if a future edit tried.

`NotificationOperationsService` is where the audit entry and the guarded
update become one transaction, so nothing here hands a route the raw port.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.admin.application.services import (
    AuditRecorder,
    NotificationOperationsService,
)
from app.modules.admin.infrastructure.repositories import SqlAlchemyAuditEntryRepository
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyAdministrativeNotificationDirectory,
)
from app.modules.notifications.public import AdministrativeNotificationDirectory


def get_admin_notification_directory(
    session: DbSessionDep,
) -> AdministrativeNotificationDirectory:
    """The per-request reader, returned as the **published read port**.

    A route holding this can list, open and batch — and cannot retry, which
    is what keeps the read surface read-only structurally rather than by
    convention.
    """
    return SqlAlchemyAdministrativeNotificationDirectory(session)


def get_notification_operations(
    session: DbSessionDep, clock: ClockDep
) -> NotificationOperationsService:
    """The one mutation, with its recorder over the same session.

    Sharing the session is what makes the re-armed delivery and its audit
    entry one transaction rather than two.
    """
    return NotificationOperationsService(
        deliveries=SqlAlchemyAdministrativeNotificationDirectory(session),
        audit=AuditRecorder(entries=SqlAlchemyAuditEntryRepository(session), clock=clock),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


AdminNotificationDirectoryDep = Annotated[
    AdministrativeNotificationDirectory, Depends(get_admin_notification_directory)
]
NotificationOperationsDep = Annotated[
    NotificationOperationsService, Depends(get_notification_operations)
]

__all__ = [
    "AdminNotificationDirectoryDep",
    "NotificationOperationsDep",
    "get_admin_notification_directory",
    "get_notification_operations",
]
