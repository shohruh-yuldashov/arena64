"""The base router — API versioning structure.

Mounted once, in app/app_factory.py, under `core.constants.API_PREFIX`.
Every API version is a router included here; a `v2_router` mounts onto this
exact object, unchanged, the day it exists — nothing about `v1`'s presence
constrains it.
"""

from fastapi import APIRouter

from app.api.v1.router import v1_router

api_router = APIRouter()
api_router.include_router(v1_router)
