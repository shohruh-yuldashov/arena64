"""Version 1 of the platform's HTTP surface.

Empty of business routes by design — this bootstrap's scope is
infrastructure only. Future modules mount their own router here
(dependency-injection.md DI-04: a module registers itself; this file is
never edited to add one).
"""

from fastapi import APIRouter

from app.api.v1.health import health_router
from app.core.constants import API_V1_PREFIX

v1_router = APIRouter(prefix=API_V1_PREFIX)
v1_router.include_router(health_router)
