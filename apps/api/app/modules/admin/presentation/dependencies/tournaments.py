"""The admin Tournaments read, wired — dependency-injection.md DI-01.

Returns the **published port**, so a route holds two reads and cannot
publish a round, advance a player or edit a bracket. A64-024.5 is read-only
because `admin.audit_entry` is unbuilt, and this is where that is
structural.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import DbSessionDep
from app.modules.tournament.infrastructure.repositories.admin_directory import (
    SqlAlchemyAdministrativeTournamentDirectory,
)
from app.modules.tournament.public.administration import AdministrativeTournamentDirectory


def get_admin_tournament_directory(
    session: DbSessionDep,
) -> AdministrativeTournamentDirectory:
    """The per-request tournament reader, over the request's session."""
    return SqlAlchemyAdministrativeTournamentDirectory(session)


AdminTournamentDirectoryDep = Annotated[
    AdministrativeTournamentDirectory, Depends(get_admin_tournament_directory)
]

__all__ = ["AdminTournamentDirectoryDep", "get_admin_tournament_directory"]
