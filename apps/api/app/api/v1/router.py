"""Version 1 of the platform's HTTP surface.

**On DI-04 and this file.** A64-006 wrote that a module "registers itself;
this file is never edited to add one", pointing at
`app.core.module_registry`. A64-010 mounts `users` here explicitly instead,
and the reason is worth stating rather than quietly reversing:

DI-04's actual value is that a module owns *its own DI bindings*, so
adding one never means editing another module's code or a shared wiring
file full of service registrations. That value is real. But `users` has no
bindings to own — its entire object graph is assembled by FastAPI
`Depends` at the presentation layer (DI-01), which is resolved per request
and never passes through a container. Routing it through the registry
would mean a `Module` class whose `configure()` method is empty, existing
only to satisfy the shape, and `app_factory` iterating a registry of one to
reach a router it could have imported directly. That is ceremony, not
decoupling — and the enumeration does not even disappear, it moves.

The registry earns its place at the first module that needs bindings
`Depends` cannot express: a Celery task, a gateway handler, or a port
bound differently per profile (dependency-injection.md §1.5). None exists
yet. Until then this is one honest, greppable line, and the trade is
recorded here rather than discovered later as an unexplained deviation.
"""

from fastapi import APIRouter

from app.api.v1.health import health_router
from app.core.constants import API_V1_PREFIX
from app.modules.auth.presentation.router import auth_router
from app.modules.users.presentation.router import users_router

v1_router = APIRouter(prefix=API_V1_PREFIX)
v1_router.include_router(health_router)
v1_router.include_router(users_router)
v1_router.include_router(auth_router)
