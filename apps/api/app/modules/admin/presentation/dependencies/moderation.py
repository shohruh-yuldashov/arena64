"""Moderation, wired — dependency-injection.md DI-01. A64-024.6.

`Depends` at the routing layer only, handing an already-resolved service to
a route. Not a container.

## Why the route gets a service and not repositories

Restricting an account carries three refusals — self-restriction, the last
administrator, a duplicate — and four writes that must commit together. A
route holding the repositories could bypass every refusal by writing a row,
and could commit the sanction without the audit entry by forgetting a line.
`ModerationService` is where both are impossible.

## Why the session revoker is adapted here

SE-3 requires a suspension to end every live session, and it must happen
inside the moderation transaction — a second transaction could commit
alone, leaving a restriction whose sessions survived or sessions revoked
for a restriction that rolled back.

`admin.application.ports.SessionRevoker` states what `admin` needs;
`auth`'s session repository does the work. The two are joined **here**,
because the composition root is where a cross-module adapter belongs and
because `admin.application` naming `RevocationReason` would make it import
another module's domain to describe a value it only passes through.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.admin.application.ports import ModerationCaseRepository
from app.modules.admin.application.services import AuditRecorder, ModerationService
from app.modules.admin.infrastructure.repositories import (
    SqlAlchemyAuditEntryRepository,
    SqlAlchemyModerationCaseRepository,
    SqlAlchemySanctionRepository,
)
from app.modules.auth.domain.sessions import RevocationReason
from app.modules.auth.infrastructure.repositories.session_repository import (
    SqlAlchemySessionRepository,
)


class SuspensionSessionRevoker:
    """`admin.application.ports.SessionRevoker`, over `auth`'s sessions.

    Binds the reason: every revocation this adapter performs is a
    suspension, and `RevocationReason.SUSPENSION` has existed unused since
    A64-011.4 waiting for exactly this caller — its docstring already says
    why ("a suspension that lets an existing socket keep playing is not a
    suspension").

    Does not commit. The session it writes through is the request's, so the
    revocation lands in whatever transaction the moderation service opened.
    """

    def __init__(self, sessions: SqlAlchemySessionRepository) -> None:
        self._sessions = sessions

    async def revoke_all_for(self, user_id: UUID, *, at: datetime) -> int:
        return await self._sessions.revoke_all_sessions(
            user_id, at=at, reason=RevocationReason.SUSPENSION
        )


def get_moderation_service(session: DbSessionDep, clock: ClockDep) -> ModerationService:
    """The per-request moderation service.

    Every collaborator shares the request's session, which is what makes
    the case, the sanction, the session revocation and the audit entry one
    transaction rather than four.
    """
    return ModerationService(
        cases=SqlAlchemyModerationCaseRepository(session),
        sanctions=SqlAlchemySanctionRepository(session),
        sessions=SuspensionSessionRevoker(SqlAlchemySessionRepository(session)),
        audit=AuditRecorder(entries=SqlAlchemyAuditEntryRepository(session), clock=clock),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def get_moderation_case_reader(session: DbSessionDep) -> ModerationCaseRepository:
    """The case batch read the console composes its responses from.

    Returned as the **port**, so a route can read a decision and cannot
    write one — §13.2's immutability, expressed in what the route is given
    rather than in what it remembers not to call.
    """
    return SqlAlchemyModerationCaseRepository(session)


ModerationServiceDep = Annotated[ModerationService, Depends(get_moderation_service)]
ModerationCaseReaderDep = Annotated[ModerationCaseRepository, Depends(get_moderation_case_reader)]

__all__ = [
    "ModerationCaseReaderDep",
    "ModerationServiceDep",
    "SuspensionSessionRevoker",
    "get_moderation_case_reader",
    "get_moderation_service",
]
