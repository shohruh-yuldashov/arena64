"""The `auth` infrastructure layer — adapters realising the ports in
`application/ports.py`.

Notably contains **no repository and no ORM model**: this module stores
nothing of its own in A64-011.1. Credentials are persisted by `users`
through its published port; the only adapter here is the password hasher.
"""

from app.modules.auth.infrastructure.password_hasher import (
    Argon2idPasswordHasher,
    build_password_hasher,
)

__all__ = ["Argon2idPasswordHasher", "build_password_hasher"]
