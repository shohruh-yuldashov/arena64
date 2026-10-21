"""Signing in with Google — the values the flow carries, A64-031.E1.

OpenID Connect's authorization-code flow with PKCE (RFC 7636) and a nonce
(OIDC Core §3.1.2.1), across two browser round trips:

    start      this platform mints `state`, `nonce` and a PKCE verifier,
               remembers them as a `StartedSignIn`, and sends the browser
               to Google holding only `state` and the verifier's challenge
    callback   Google sends the browser back with `state` and a one-time
               `code`. The `StartedSignIn` is taken — once — and the code
               is exchanged, with the verifier, for an ID token whose
               signature, issuer, audience, expiry and nonce are checked

If the verified identity is already linked, the browser is signed in. If
not, it is a `PendingSignIn`: a verified identity waiting for one more
step the person must take — choosing a username, or proving they own the
Arena64 account that already uses the address. See ADR-008.

Nothing here touches I/O; the records are stored by
`infrastructure/external_sign_in_store.py`.
"""

import base64
import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from uuid import UUID

#: Where a sign-in lands when it was not sent from anywhere in particular.
DEFAULT_NEXT_PATH: Final = "/"
#: Long enough for any in-app path; short enough not to be a payload.
NEXT_PATH_MAX_LENGTH: Final = 512


class PendingPurpose(StrEnum):
    """The step a verified Google identity is waiting for."""

    CREATE_ACCOUNT = "create_account"
    """No Arena64 account uses the address: choose a username."""

    LINK_ACCOUNT = "link_account"
    """An Arena64 account already uses the address: prove it is yours with
    its password. **Never** linked on the address alone — ADR-008."""


class SignInFailure(StrEnum):
    """Why a callback did not sign anybody in. The value is what the
    browser is sent back to the app with, so it is a stable wire string."""

    CANCELLED = "cancelled"
    """The person declined at Google. Not an error, and not shown as one."""

    EXPIRED = "expired"
    """The `state` was unknown, used, or out of time — or arrived in a
    browser other than the one that started it. Start again."""

    FAILED = "failed"
    """Google could not be reached, or what it returned did not verify."""

    EMAIL_UNVERIFIED = "email_unverified"
    """Google would not vouch for the address, and nothing is linked yet:
    there is no account this identity may create or claim."""


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    """What a verified ID token asserted. Only `subject` identifies anyone;
    the address is a hint for the first sign-in and nothing more."""

    subject: str
    email: str | None
    email_verified: bool
    name: str | None = None


@dataclass(frozen=True, slots=True)
class StartedSignIn:
    """What `start` remembers, under the digest of `state`.

    `binding_digest` ties it to the browser that started it: the raw value
    is in an `HttpOnly` cookie only that browser holds, so a `state`
    lifted from somebody else's URL is useless without it. The other two
    secrets never leave the server until the token exchange.
    """

    binding_digest: bytes = field(repr=False)
    code_verifier: str = field(repr=False)
    nonce: str = field(repr=False)
    next_path: str = DEFAULT_NEXT_PATH


@dataclass(frozen=True, slots=True)
class PendingSignIn:
    """A verified identity waiting for its next step, under the digest of
    a ticket held in an `HttpOnly` cookie."""

    purpose: PendingPurpose
    subject: str = field(repr=False)
    email: str = field(repr=False)
    user_id: UUID | None = None
    """The account to link — `LINK_ACCOUNT` only. Fixed at the callback, so
    the password step cannot be pointed at a different account."""
    next_path: str = DEFAULT_NEXT_PATH


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 §4.2's `S256`: base64url of SHA-256, unpadded. `plain` is
    never offered — it gives away the verifier to anybody who sees the
    authorisation URL."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def safe_next_path(candidate: str | None) -> str:
    """An in-app path to land on afterwards, or `/`.

    The browser is sent to `PUBLIC_APP_URL` + this, so the origin is never
    the caller's to choose — but a value that does not start with a single
    `/` could still change it (`@evil.example` makes the configured origin
    a userinfo prefix), so anything that is not plainly a path is refused.
    The frontend applies the same rule (`safe-redirect.ts`) before it ever
    sends one; this is the server's copy, for a link built by hand.
    """
    if not candidate or len(candidate) > NEXT_PATH_MAX_LENGTH:
        return DEFAULT_NEXT_PATH
    if not candidate.startswith("/") or candidate.startswith("//"):
        return DEFAULT_NEXT_PATH
    if "\\" in candidate or not candidate.isprintable() or any(c.isspace() for c in candidate):
        return DEFAULT_NEXT_PATH
    return candidate
