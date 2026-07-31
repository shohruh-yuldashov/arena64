"""`RefreshTokenService` — refresh-token naming over the shared token
mechanics.

A64-011.4 implemented generate/hash/verify here directly. A64-011.6
needed byte-identical operations for email verification tokens, so the
mechanics moved to `OpaqueTokenService` and this class became the
refresh-token *vocabulary* over them — see that module for the DB-24
reasoning behind SHA-256, the 256-bit floor and the absence of a salt.

**Why this class still exists rather than every caller using
`OpaqueTokenService` directly.** `SessionService` is written against
`generate_refresh_token` / `hash_refresh_token` / `verify_refresh_token`,
and those names are what make its call sites readable — a bare `hash()`
in the middle of a rotation says nothing about *what* is being hashed.
It is also the seam where a refresh token could gain a prefix, a version
marker or a different length without any other caller of the shared
service noticing.

The entropy comes from `SessionSettings`, so refresh tokens can be
lengthened independently of verification tokens.
"""

from app.config.settings import SessionSettings
from app.modules.auth.application.services.opaque_tokens import OpaqueTokenService


class RefreshTokenService:
    """Stateless. Holds settings and nothing else — no clock, no storage,
    no session knowledge. It turns randomness into strings and strings
    into digests, and `SessionService` decides what any of it means."""

    def __init__(self, settings: SessionSettings) -> None:
        self._tokens = OpaqueTokenService(settings.token_entropy_bytes)

    def generate_refresh_token(self) -> str:
        """A new, unguessable refresh token — `OpaqueTokenService.generate`."""
        return self._tokens.generate()

    def hash_refresh_token(self, token: str) -> bytes:
        """The digest stored in `auth.user_sessions.refresh_token_hash`."""
        return self._tokens.hash(token)

    def verify_refresh_token(self, token: str, expected_hash: bytes) -> bool:
        """Constant-time comparison — `OpaqueTokenService.verify`."""
        return self._tokens.verify(token, expected_hash)
