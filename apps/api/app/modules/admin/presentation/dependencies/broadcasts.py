"""Broadcast composition, wired — dependency-injection.md DI-01. A64-027A.

Two dependencies, and the split matters for the same reason it does next
door in `notifications.py`: the audit recorder is handed to the route
separately from the service, so the entry is written by the transport that
knows who the actor is rather than by a service that would have to be told.

The service is assembled with the **real** audience directory and the real
clock. There is no test double reachable from here — a build in which a
broadcast could be constructed without `users`' eligibility rule would be a
build where "who may be announced to" depends on the wiring.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.admin.application.services import AuditRecorder
from app.modules.admin.infrastructure.repositories import SqlAlchemyAuditEntryRepository
from app.modules.notifications.application.services.broadcast_service import BroadcastService
from app.modules.notifications.infrastructure.repositories.broadcast_repository import (
    SqlAlchemyBroadcastRepository,
)
from app.modules.users.infrastructure.repositories.audience_directory import (
    SqlAlchemyNotificationAudienceDirectory,
)


def get_broadcast_service(session: DbSessionDep, clock: ClockDep) -> BroadcastService:
    return BroadcastService(
        repository=SqlAlchemyBroadcastRepository(session),
        audience=SqlAlchemyNotificationAudienceDirectory(session),
        clock=clock,
        unit_of_work=SessionUnitOfWork(session),
    )


def get_audit_recorder(session: DbSessionDep, clock: ClockDep) -> AuditRecorder:
    return AuditRecorder(entries=SqlAlchemyAuditEntryRepository(session), clock=clock)


BroadcastServiceDep = Annotated[BroadcastService, Depends(get_broadcast_service)]
AuditRecorderDep = Annotated[AuditRecorder, Depends(get_audit_recorder)]

__all__ = ["AuditRecorderDep", "BroadcastServiceDep", "get_audit_recorder", "get_broadcast_service"]
