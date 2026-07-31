"""The ports `auth`'s use cases program against — AD-06: declared in
`application/`, satisfied by `infrastructure/`.
"""

from collections.abc import Mapping
from typing import Any, Protocol

from app.modules.auth.domain.tokens import TokenClaims, TokenType


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


class TokenProvider(Protocol):
    """Signs and verifies tokens. Knows nothing about *why* one is issued.

    The deliberate split from `AccessTokenService` and `TokenValidator`:
    this port is about cryptography and encoding, those are about use
    cases. `AccessTokenService` decides that an access token lasts fifteen
    minutes and speaks for a particular account; this decides only how a
    claim set becomes a signed string and back. That line is what lets
    A64-011.4 add refresh tokens, and a later task add WebSocket tickets
    (AD-09), by writing a new *service* against this same port rather than
    a second signing implementation — and two independent signing
    implementations on one platform is how one of them ends up without
    audience checking.

    A `Protocol`, not an ABC, so a test can substitute a deterministic
    fake without inheriting from the real thing.

    **Synchronous on purpose.** HMAC-SHA256 over a few hundred bytes is
    microseconds — four orders of magnitude below the ~20ms Argon2
    verification that forced `PasswordHasher` onto a worker thread. Making
    these `async` would buy nothing and would cost an `await` on the hot
    path of every authenticated request on the platform.
    """

    def issue(
        self,
        *,
        subject: str,
        token_type: TokenType,
        lifetime_seconds: int,
        custom: Mapping[str, Any] | None = None,
    ) -> tuple[str, TokenClaims]:
        """Mints a signed token and returns it alongside the claims it
        carries.

        Both, rather than just the string, because the caller almost
        always needs the `exp` it just created — a login response has to
        tell the client when to refresh, and re-decoding a token to learn
        what you put in it a microsecond ago is absurd.

        `jti` and `iat` are generated here, not accepted as arguments: a
        caller that could choose a token's own identifier could mint two
        tokens with the same one, which would make a `jti` denylist
        (A64-011.4) silently revoke the wrong token.
        """
        ...

    def decode(self, token: str, *, expected_type: TokenType) -> TokenClaims:
        """Verifies a token completely, or raises.

        Verifies, in the order that matters: the signature (against every
        active key — see `JWTSettings.previous_secret_keys`), then `exp`,
        `iss`, `aud`, and finally `expected_type`. There is no partial
        result and no "skip verification" argument; holding a `TokenClaims`
        means every one of those passed.

        `expected_type` is required rather than defaulted, because the
        default anyone would pick is `ACCESS`, and the one call site that
        forgets to override it is the one that accepts a refresh token
        where an access token belongs.

        Raises `ExpiredToken`, `InvalidSignature`, or `InvalidToken` —
        never a `jwt.*` exception, which would leak the library's
        vocabulary into every caller and make swapping it a rewrite.
        """
        ...
