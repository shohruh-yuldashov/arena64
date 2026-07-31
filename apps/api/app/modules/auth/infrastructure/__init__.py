"""The `auth` infrastructure layer — adapters realising the ports in
`application/ports.py`.

A64-011.1 noted here that this module "stores nothing of its own":
credentials were persisted by `users` through its published port, and the
password hasher was the only adapter. A64-011.4 changed that. A refresh
session is `auth`'s own state — it is created, rotated and revoked by
`auth` alone and no other module reads it — so it gets `auth`'s own table
in `auth`'s own schema (database.md §3.1).
"""

from app.modules.auth.infrastructure.jwt_token_provider import JwtTokenProvider
from app.modules.auth.infrastructure.password_hasher import (
    Argon2idPasswordHasher,
    build_password_hasher,
)
from app.modules.auth.infrastructure.repositories import SqlAlchemySessionRepository

__all__ = [
    "Argon2idPasswordHasher",
    "JwtTokenProvider",
    "SqlAlchemySessionRepository",
    "build_password_hasher",
]
