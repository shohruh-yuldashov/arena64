"""`RefreshTokenService` — generate, hash, verify. Nothing else.

Three small functions with an outsized amount of reasoning behind them,
because every one of the three is a place this goes quietly wrong.

## Why SHA-256 and not Argon2id — DB-24

The platform hashes passwords with Argon2id and refresh tokens with
SHA-256, and database.md DB-24 is explicit that this is not sloppiness:

> Argon2id's cost exists to defeat brute force against a small guessable
> space. A 256-bit random token has no such space — an attacker with the
> hash gains nothing from any amount of computation. Applying Argon2id to
> refresh tokens would put tens of milliseconds of deliberate work on the
> token-refresh path, which every connected client executes repeatedly,
> in exchange for no additional security. Applying SHA-256 to passwords
> would be a serious defect. Same operation, opposite reasoning, and
> getting them backwards is common.

The load-bearing premise is *256 bits from a CSPRNG*. SHA-256 over a
token with less entropy than that would be a real weakness, which is why
`SessionSettings.token_entropy_bytes` has a hard floor rather than a
default someone can lower.

No salt, and that is deliberate too. A salt defends against precomputed
tables over a guessable space; there is no such space here, and an
unsalted digest is what makes the hash a *lookup key* — the refresh path
finds a session by hashing the presented token and querying for it once,
rather than fetching candidate rows and comparing each. A salted scheme
would turn one indexed lookup into a scan.

## Why there is no `secrets.compare_digest` in the lookup path

There is one in `verify_refresh_token`, and it is used where a comparison
genuinely happens. But the primary path does not compare at all: it
hashes and looks the digest up in a unique index. That is constant-time
with respect to the token by construction — the database is comparing
digests, not secrets, and a digest reveals nothing by leaking its
comparison time because the attacker would have to invert SHA-256 to use
what leaked.
"""

import hashlib
import hmac
import secrets

from app.config.settings import SessionSettings


class RefreshTokenService:
    """Stateless. Holds settings and nothing else — no clock, no storage,
    no session knowledge. It turns randomness into strings and strings
    into digests, and `SessionService` decides what any of it means."""

    def __init__(self, settings: SessionSettings) -> None:
        self._settings = settings

    def generate_refresh_token(self) -> str:
        """A new, unguessable token.

        `secrets.token_urlsafe`, not `random`, not `uuid4`, not
        `os.urandom` formatted by hand:

        - `random` is a Mersenne Twister seeded from the clock, and
          observing a few outputs lets an attacker predict every
          subsequent one. It is not a security failure that shows up in
          testing; it is one that shows up in an incident report.
        - `uuid4` carries 122 bits, not 256, and six of its bits are
          fixed version/variant markers. Under the threshold DB-24's
          reasoning depends on.
        - `secrets` draws from the OS CSPRNG, which is what "cryptographically
          secure" means here.

        URL-safe base64, so the token survives a `Set-Cookie` header, a
        query string and a JSON body without escaping — a token that has
        to be encoded somewhere is a token that eventually gets compared
        in two different encodings.

        `token_urlsafe(n)` returns roughly `4n/3` characters carrying `8n`
        bits; at the configured 32 bytes that is 43 characters and 256
        bits.
        """
        return secrets.token_urlsafe(self._settings.token_entropy_bytes)

    def hash_refresh_token(self, token: str) -> bytes:
        """The digest that gets stored — DB-24.

        Returns `bytes`, not a hex string, because the column is `bytea`
        and because a hex rendering is a second representation of the same
        value that something will eventually compare against the first.
        The one bug that costs a full day is `"a1b2..." != b"\\xa1\\xb2..."`
        silently never matching.

        Deterministic and unsalted, so this doubles as the lookup key —
        see this module's docstring.
        """
        return hashlib.sha256(token.encode("utf-8")).digest()

    def verify_refresh_token(self, token: str, expected_hash: bytes) -> bool:
        """Whether `token` is the one `expected_hash` was made from.

        `hmac.compare_digest`, never `==`. The comparison is of two
        digests rather than two secrets, so the timing leak here is
        weaker than it would be on a password — but "weaker" is not
        "absent", and the cost of doing it right is a function call.
        Writing `==` in a credential path is also the kind of thing that
        gets copied into a place where it does matter.

        Returns a plain `bool` rather than raising: a token that does not
        match is an ordinary outcome, and the caller has to treat it
        identically to "no such session" anyway (see `SessionService`).
        Symmetry in the return type keeps the branches symmetric in the
        caller.
        """
        return hmac.compare_digest(self.hash_refresh_token(token), expected_hash)
