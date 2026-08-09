"""The admin Users read, wired — dependency-injection.md DI-01.

Returns the **published port**, so a route holds two reads and could not
change an account even if it tried. That is what makes A64-024.3's
read-only decision structural rather than a convention: there is no write
on the object the route is given.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import DbSessionDep
from app.modules.users.infrastructure.repositories.user_repository import (
    SqlAlchemyAdministrativeUserDirectory,
)
from app.modules.users.public import AdministrativeUserDirectory


def get_admin_user_directory(session: DbSessionDep) -> AdministrativeUserDirectory:
    """The per-request account reader, over the request's session."""
    return SqlAlchemyAdministrativeUserDirectory(session)


AdminUserDirectoryDep = Annotated[AdministrativeUserDirectory, Depends(get_admin_user_directory)]

__all__ = ["AdminUserDirectoryDep", "get_admin_user_directory"]
