"""The admin Matches read, wired — dependency-injection.md DI-01.

Returns the **published port**, so a route holds two reads and cannot alter
a match. A64-024.4 is read-only because `admin.audit_entry` is unbuilt, and
this is where that is structural rather than a convention.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import DbSessionDep
from app.modules.game.infrastructure.repositories.match_record_repository import (
    SqlAlchemyAdministrativeMatchDirectory,
)
from app.modules.game.public import AdministrativeMatchDirectory


def get_admin_match_directory(session: DbSessionDep) -> AdministrativeMatchDirectory:
    """The per-request match reader, over the request's session."""
    return SqlAlchemyAdministrativeMatchDirectory(session)


AdminMatchDirectoryDep = Annotated[AdministrativeMatchDirectory, Depends(get_admin_match_directory)]

__all__ = ["AdminMatchDirectoryDep", "get_admin_match_directory"]
