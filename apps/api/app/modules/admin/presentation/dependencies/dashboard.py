"""The dashboard, wired — dependency-injection.md DI-01. A64-024.9.

Six collaborators, every one a **published read port** of the module that
owns the fact it answers for. `admin` composes; it does not query another
module's tables.

The two `admin`-owned readers are the repositories for its own schema — the
sanction count and the audit page — which is the same distinction every
other file here draws.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.modules.admin.application.services.dashboard_service import DashboardService
from app.modules.admin.infrastructure.repositories import (
    SqlAlchemyAuditEntryRepository,
    SqlAlchemySanctionRepository,
)
from app.modules.game.infrastructure.repositories.match_record_repository import (
    SqlAlchemyAdministrativeMatchDirectory,
)
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyAdministrativeNotificationDirectory,
)
from app.modules.tournament.infrastructure.repositories.admin_directory import (
    SqlAlchemyAdministrativeTournamentDirectory,
)
from app.modules.users.infrastructure.repositories.user_repository import (
    SqlAlchemyAdministrativeUserDirectory,
)


def get_dashboard_service(session: DbSessionDep, clock: ClockDep) -> DashboardService:
    """The per-request overview reader, over the request's session.

    One session for all seven reads: they are sequential by design (a
    session is not safe to use concurrently), and a connection per card
    would open six to render one page.
    """
    return DashboardService(
        accounts=SqlAlchemyAdministrativeUserDirectory(session),
        matches=SqlAlchemyAdministrativeMatchDirectory(session),
        tournaments=SqlAlchemyAdministrativeTournamentDirectory(session),
        sanctions=SqlAlchemySanctionRepository(session),
        notifications=SqlAlchemyAdministrativeNotificationDirectory(session),
        audit=SqlAlchemyAuditEntryRepository(session),
        clock=clock,
    )


DashboardServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]

__all__ = ["DashboardServiceDep", "get_dashboard_service"]
