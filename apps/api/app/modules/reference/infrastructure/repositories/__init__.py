"""`reference`'s repositories. Private — see `reference.public`."""

from app.modules.reference.infrastructure.repositories.time_control_repository import (
    SqlAlchemyTimeControlCatalogue,
)

__all__ = ["SqlAlchemyTimeControlCatalogue"]
