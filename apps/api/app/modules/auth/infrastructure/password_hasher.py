"""Argon2id password hashing.

Argon2id specifically — not Argon2i, not Argon2d, and not bcrypt/PBKDF2.
Argon2id is the hybrid the RFC 9106 authors recommend for password
storage: it resists both the side-channel attacks Argon2d is exposed to
and the time-memory trade-off attacks Argon2i is exposed to. It is also
what database.md §14.2 already specified for this platform before there
was any code to hash with.

## Why the hashing runs in a worker thread

Argon2id is *designed* to be slow and memory-hungry — that is the entire
security property. At this platform's parameters a single hash costs on
the order of tens of milliseconds of solid CPU inside a C extension.

Called directly from an async request handler, that blocks the event
loop: not just the caller's request, but **every** concurrent request on
that worker process, including live match traffic whose whole latency
budget is 25ms (system-design.md CP-1/T-1). Ten simultaneous
registrations would stall the loop for half a second.

`anyio.to_thread.run_sync` moves each hash onto a worker thread, where
the GIL is released for the duration of the C call, so the loop stays
free. This is why `PasswordHasher.hash` is `async` even though the
underlying library is synchronous — the port's shape is dictated by the
cost of the operation, not by the library's API.

## Why the parameters are not stored separately

An Argon2 encoded hash embeds its own algorithm, version and cost
parameters:

    $argon2id$v=19$m=19456,t=2,p=1$<salt>$<digest>

so the "per-row parameters" database.md §14.2 requires — "let a sign-in
verify against the parameters the hash was made with and transparently
rehash at the current settings" — are already in the one `password_hash`
column. No companion columns are needed, and adding them would create a
second source of truth that could disagree with the string itself.
"""

from anyio import to_thread
from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2 import Type

from app.config.settings import AuthSettings


class Argon2idPasswordHasher:
    """The production `PasswordHasher`.

    Constructed once per process at the composition root: the underlying
    `argon2.PasswordHasher` is stateless and thread-safe, and building one
    per request would be pure waste on the one code path already spending
    tens of milliseconds.
    """

    def __init__(self, settings: AuthSettings) -> None:
        self._hasher = Argon2PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
            # `Type.ID` is the whole point — argon2-cffi's default is
            # already Argon2id, but stating it means a library default
            # change cannot silently move this platform onto a variant
            # that is wrong for password storage.
            type=Type.ID,
        )

    async def hash(self, plaintext: str) -> str:
        """Hashes off the event loop — see this module's docstring.

        A fresh cryptographically-random salt is generated per call by
        argon2-cffi and embedded in the returned encoding, so two
        identical passwords never produce the same hash and a stolen
        database cannot be attacked with a precomputed table.
        """
        return await to_thread.run_sync(self._hasher.hash, plaintext)
