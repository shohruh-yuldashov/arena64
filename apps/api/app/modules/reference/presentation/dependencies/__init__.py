"""The FastAPI `Depends` bridge for `reference` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved reader. It is not the container.

One factory, because there is one read. It returns the **port**, so a route
annotating this dependency holds `active` and `require` and could not reach
a writer even if one existed — which is the property `reference.public`
exists to keep (a catalogue is seeded by a migration, never by a request).
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import DbSessionDep
from app.modules.reference.infrastructure.repositories import SqlAlchemyTimeControlCatalogue
from app.modules.reference.public import TimeControlCatalogue


def get_time_control_catalogue(session: DbSessionDep) -> TimeControlCatalogue:
    """The per-request catalogue reader, over the request's session."""
    return SqlAlchemyTimeControlCatalogue(session)


TimeControlCatalogueDep = Annotated[TimeControlCatalogue, Depends(get_time_control_catalogue)]

__all__ = ["TimeControlCatalogueDep", "get_time_control_catalogue"]
