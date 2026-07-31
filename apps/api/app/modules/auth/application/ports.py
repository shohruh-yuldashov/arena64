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

    A64-011.1 published `hash` alone, on the grounds that an unused method
    on a security interface reads as "this is wired up" to whoever adds
    login next. A64-011.2 is that task, so `verify` and `needs_rehash`
    join it here — together, because a `verify` without a `needs_rehash`
    silently freezes every account at whatever parameters it was first
    hashed with.
    """

    async def hash(self, plaintext: str) -> str:
        """Returns the encoded hash — algorithm, parameters, salt and
        digest in one string. Never returns or logs the plaintext."""
        ...

    async def verify(self, encoded_hash: str, plaintext: str) -> bool:
        """Whether `plaintext` is the password `encoded_hash` was made from.

        Returns a plain `bool` rather than raising on mismatch: a wrong
        password is the single most ordinary outcome this platform has,
        and the caller has to treat "no such account" and "wrong password"
        identically anyway (see `AuthenticationService`). An exception for
        one and not the other is how that symmetry gets broken by accident.

        A *malformed* stored hash is different and does raise — it means
        the database holds something that is not a credential, which no
        sign-in logic can recover from and operators must be told about.

        The comparison is constant-time with respect to the digest; the
        implementation must not compare hashes with `==`.
        """
        ...

    async def needs_rehash(self, encoded_hash: str) -> bool:
        """Whether `encoded_hash` was made with weaker parameters than the
        ones currently configured.

        Reading the parameters back out of the encoding is what makes
        raising Argon2's cost possible at all without a mass password
        reset (database.md §14.2): a sign-in verifies against the
        parameters the hash was made with, and this says whether to
        re-derive it at today's.
        """
        ...

    async def dummy_hash(self) -> str:
        """A hash of nothing in particular, at the current parameters, for
        a caller that must spend a verification's worth of time without
        having a real credential to verify against.

        On the port rather than left to the caller because getting it
        wrong is silent: a hardcoded constant would drift from the
        configured cost the moment those are raised, and the timing
        equalisation it exists to provide would quietly stop working with
        nothing failing. The implementation that owns the parameters is
        the one that can keep it honest.

        Never a real user's hash, and never derived from any real
        password.
        """
        ...
