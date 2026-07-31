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

import secrets
from functools import lru_cache

from anyio import to_thread
from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config.settings import AuthSettings
from app.core.exceptions import PermanentInfrastructureError


class Argon2idPasswordHasher:
    """The production `PasswordHasher`.

    Shared per process — see `build_password_hasher`, which is how the
    composition root should obtain one. The underlying
    `argon2.PasswordHasher` is stateless and thread-safe; the only
    instance state is the memoised dummy hash, and that memo is the
    reason sharing matters.
    """

    def __init__(self, settings: AuthSettings) -> None:
        self._dummy_hash: str | None = None
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

    async def verify(self, encoded_hash: str, plaintext: str) -> bool:
        """Verifies off the event loop, for the same reason `hash` does —
        verification costs the same as hashing by construction.

        The digest comparison itself is argon2-cffi's, which uses the
        library's constant-time equality. Nothing here ever sees the two
        digests, let alone compares them with `==`.
        """
        try:
            return await to_thread.run_sync(self._hasher.verify, encoded_hash, plaintext)
        except VerifyMismatchError:
            # The ordinary "wrong password". Must be caught before
            # `VerificationError`, which it subclasses.
            return False
        except (InvalidHashError, VerificationError) as error:
            # The stored string is not a usable Argon2 encoding: truncated
            # by a bad migration, written by something that was not this
            # hasher, or corrupted. That is a defect in stored data, not a
            # failed sign-in — reporting it as "wrong password" would leave
            # a person permanently unable to log in with the right one
            # while every dashboard showed a normal failure rate.
            #
            # `Permanent`, not `Transient`: retrying reads the same bad
            # bytes. The message names no user and carries no hash.
            raise PermanentInfrastructureError(
                "Stored password hash is not a valid Argon2 encoding."
            ) from error

    async def needs_rehash(self, encoded_hash: str) -> bool:
        """Cheap — parses the encoding's parameter header and compares it
        against this instance's configuration. No key derivation runs, so
        unlike `hash` and `verify` this needs no worker thread.
        """
        return self._hasher.check_needs_rehash(encoded_hash)

    async def dummy_hash(self) -> str:
        """Hashes an unguessable per-process string, once, and reuses it.

        Computed lazily rather than in `__init__`, which cannot await, and
        cached because the whole point is to spend *one* verification's
        time per unknown-address attempt — deriving a fresh dummy on every
        one would double the cost of exactly the requests an attacker
        controls the volume of.

        Two concurrent first calls may both compute one. That race is
        harmless: either value is equally valid, and a lock on the cold
        path would be more machinery than the duplicated work it saves.

        The plaintext is `secrets.token_urlsafe(32)` — 256 bits from the
        OS CSPRNG, never persisted, never logged, and different in every
        process. Nobody can submit a password that verifies against it,
        which is what makes "verify against the dummy" always fail without
        being a special case.
        """
        if self._dummy_hash is None:
            self._dummy_hash = await self.hash(secrets.token_urlsafe(32))
        return self._dummy_hash


@lru_cache(maxsize=8)
def _cached_hasher(
    time_cost: int,
    memory_cost_kib: int,
    parallelism: int,
) -> Argon2idPasswordHasher:
    """Keyed on the three cost parameters as plain ints — deliberately not
    on the `AuthSettings` object, which is a Pydantic model and not
    reliably hashable (A64-011.1 hit exactly that and removed the cache
    rather than work around it)."""
    return Argon2idPasswordHasher(
        AuthSettings(
            argon2_time_cost=time_cost,
            argon2_memory_cost_kib=memory_cost_kib,
            argon2_parallelism=parallelism,
        )
    )


def build_password_hasher(settings: AuthSettings) -> Argon2idPasswordHasher:
    """Returns the process's shared hasher for these parameters.

    A64-011.1 deliberately built one per request, having measured
    construction at 1 µs against the ~19,000 µs of the hash itself, and
    concluded the cache was optimising nothing. That reasoning was about
    cost, and it was right about cost.

    A64-011.2 brings the singleton back for a different reason, which cost
    does not reach: `dummy_hash` must be computed **once per process**.
    Derived per request instead, a sign-in for an unknown address would
    spend two Argon2 operations (build the dummy, then verify against it)
    where a known address spends one — reintroducing, at double
    magnitude and with the sign flipped, the very timing difference the
    dummy exists to erase. Sharing the instance is what makes the memo
    real.
    """
    return _cached_hasher(
        settings.argon2_time_cost,
        settings.argon2_memory_cost_kib,
        settings.argon2_parallelism,
    )
