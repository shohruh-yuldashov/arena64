"""Tournament administration, wired — dependency-injection.md DI-01.

The composition root for A64-024.5H, and the file where two module
boundaries are crossed on purpose.

## Why the adapter lives here

`admin.application.ports.TournamentLifecycle` states what `admin` needs;
`tournament`'s own application services do the work. Joining them is the
composition root's job, exactly as `app/operator/tournament.py` joins the
same services for the shell — the two reach one graph, and neither
reimplements a transition.

## Why the lifecycle services are handed a `ParticipatingUnitOfWork`

They commit by contract, which is right for the operator command line: it
has nothing to add to their transaction. The admin console has the audit
entry, and A64-024.8's invariant is that the two land together.

`ParticipatingUnitOfWork` (A64-022.3 §10) is the repository's existing
answer: the inner service still has a scope and still rolls back on
failure, but does not end the transaction. `TournamentAdministrationService`
holds the `SessionUnitOfWork` that does.

Nothing about `tournament` changes to make this possible, which is the
point — the *caller* that needs to own the transaction says so by handing
over a unit of work that does not finish one.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ClockDep, DbSessionDep, SettingsDep
from app.config.settings import TournamentSettings
from app.core.clock import Clock
from app.database.unit_of_work import ParticipatingUnitOfWork, SessionUnitOfWork
from app.modules.admin.application.ports import TournamentLifecycleResult
from app.modules.admin.application.services import AuditRecorder
from app.modules.admin.application.services.tournament_administration_service import (
    TournamentAdministrationService,
)
from app.modules.admin.infrastructure.repositories import SqlAlchemyAuditEntryRepository
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.presentation.dependencies import build_match_creation
from app.modules.rating.public import SpeedClass
from app.modules.tournament.domain.tournament import TournamentStatus
from app.modules.tournament.presentation.dependencies import (
    build_registration_service,
    build_start_service,
)
from app.modules.users.application.services.user_service import UserService
from app.modules.users.infrastructure.repositories.user_repository import SqlAlchemyUserRepository
from app.modules.users.public import UserProfileService
from app.platform.outbox import OutboxEventPublisher, SqlAlchemyOutboxRepository


class TournamentLifecycleAdapter:
    """`admin.application.ports.TournamentLifecycle`, over `tournament`.

    Four methods, each one call into a service that already owns the rule.
    There is no validation here and no orchestration: the transition table
    is the aggregate's, seeding is the seeding service's, and the row lock
    that makes two administrators safe is the repository's.

    Returns the narrow result `admin` declared rather than the aggregate,
    so a tournament's shape does not cross into a module that only needs to
    know what state it reached.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: TournamentSettings,
        clock: Clock,
    ) -> None:
        events = OutboxEventPublisher(SqlAlchemyOutboxRepository(session))
        # Staged, not committed — see this module's docstring.
        staging = ParticipatingUnitOfWork(session)

        self._registration = build_registration_service(
            session,
            players=UserProfileService(
                UserService(
                    users=SqlAlchemyUserRepository(session),
                    unit_of_work=staging,
                    clock=clock,
                )
            ),
            events=events,
            clock=clock,
            unit_of_work=staging,
        )
        self._start = build_start_service(
            session,
            # Staged too: `MatchCreationService` commits per match by
            # contract, which is right for the pairing path and would
            # otherwise commit this transition before its audit entry was
            # written.
            matches=build_match_creation(session, events=events, clock=clock, unit_of_work=staging),
            settings=settings,
            events=events,
            clock=clock,
            unit_of_work=staging,
        )

    async def create(
        self,
        *,
        name: str,
        variant: ProductVariant,
        speed_class: SpeedClass,
        capacity: int,
        rated: bool,
        registration_deadline: datetime | None,
        created_by: UUID,
    ) -> TournamentLifecycleResult:
        created = await self._registration.create(
            name=name,
            variant=variant,
            speed_class=speed_class,
            capacity=capacity,
            rated=rated,
            created_by=created_by,
            registration_deadline=registration_deadline,
        )
        return TournamentLifecycleResult(tournament_id=created.id, status=created.status)

    async def open_registration(self, tournament_id: UUID) -> TournamentLifecycleResult:
        moved = await self._registration.open_registration(tournament_id)
        return TournamentLifecycleResult(tournament_id=moved.id, status=moved.status)

    async def close_registration(self, tournament_id: UUID) -> TournamentLifecycleResult:
        moved = await self._registration.close_registration(tournament_id)
        return TournamentLifecycleResult(tournament_id=moved.id, status=moved.status)

    async def start(self, tournament_id: UUID) -> TournamentLifecycleResult:
        launched = await self._start.start_tournament(tournament_id)
        # The status is `IN_PROGRESS` by construction: the service raises
        # `TournamentNotStartable` for every other origin, so reaching this
        # line means the transition happened.
        return TournamentLifecycleResult(
            tournament_id=tournament_id,
            status=TournamentStatus.IN_PROGRESS,
            matches_launched=len(launched),
        )


def get_tournament_administration(
    session: DbSessionDep, settings: SettingsDep, clock: ClockDep
) -> TournamentAdministrationService:
    """The per-request administration service.

    Every collaborator shares the request's session, which is what lets the
    transition and its audit entry be one transaction.
    """
    return TournamentAdministrationService(
        lifecycle=TournamentLifecycleAdapter(session, settings=settings.tournament, clock=clock),
        audit=AuditRecorder(entries=SqlAlchemyAuditEntryRepository(session), clock=clock),
        unit_of_work=SessionUnitOfWork(session),
    )


TournamentAdministrationDep = Annotated[
    TournamentAdministrationService, Depends(get_tournament_administration)
]

__all__ = [
    "TournamentAdministrationDep",
    "TournamentLifecycleAdapter",
    "get_tournament_administration",
]
