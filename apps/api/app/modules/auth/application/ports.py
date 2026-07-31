"""The ports `auth`'s use cases program against — AD-06: declared in
`application/`, satisfied by `infrastructure/`.
"""

from typing import Protocol


class PasswordHasher(Protocol):
    """Turns a plaintext password into a storable encoded hash.

    A port rather than a direct `argon2` import so the service can be
    tested with a fast stub — real Argon2id is *deliberately* ~50ms per
    call, and a service suite that hashes for real would spend all its
    time proving a library works rather than proving orchestration does.

    **`hash` is the only method, on purpose.** A `verify` alongside it
    would be unreachable code in A64-011.1 — nothing logs in yet — and an
    unused method on a security interface is worse than most dead code,
    because it reads as "this is wired up" to whoever adds login next.
    A64-011.2 adds `verify` and `needs_rehash` together, when both have a
    caller. See the task's recommendations for what they must do.
    """

    async def hash(self, plaintext: str) -> str:
        """Returns the encoded hash — algorithm, parameters, salt and
        digest in one string. Never returns or logs the plaintext."""
        ...
