"""RFC 8292 VAPID — proving to a push service who is sending — A64-021.6 §5.

A push endpoint is a bearer capability: anybody who has one can notify that
browser. VAPID is what lets a push service tell *this* platform's messages
from a stolen endpoint's, by requiring every request to carry a short-lived
assertion signed with a key pair whose public half the browser committed to
at subscription time.

That commitment is the important half. The browser passed our public key to
`pushManager.subscribe({ applicationServerKey })`, and the push service will
now refuse any message for that subscription not signed by the matching
private key. So a leaked endpoint is not, on its own, a way to notify
somebody.

## Consequences that are easy to miss

**Rotating the key pair invalidates every existing subscription.** Not
"eventually" — immediately, and permanently, because a subscription is bound
to the public key it was created with. There is no re-signing; every browser
must subscribe again. That makes the key pair operational state rather than
a rotatable secret, and it is why `app/operator/push_keys.py` generates one
and this platform never generates one at startup.

**The assertion is not a secret.** It is signed, not encrypted, and it says
only "this origin, expiring at this time". It carries no user, no
subscription and no payload.

## The `aud` claim

The push service's **origin** — scheme and host of the endpoint, nothing
more. Sending the full endpoint URL is a common bug: it puts the
subscription's unique path into a token, and some services reject it.
"""

import time
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.platform.push.encoding import b64url_decode, b64url_encode

#: How long an assertion is valid.
#:
#: RFC 8292 §2 caps this at 24 hours and services enforce it. Twelve is used
#: because the token is cached for the life of the process and a process can
#: outlive a day — a shorter window means a signature per message, and a
#: longer one is refused.
ASSERTION_TTL_SECONDS: Final = 12 * 60 * 60

#: Re-sign this long before expiry rather than at it, so a message in flight
#: when the clock crosses the boundary is not refused.
RENEW_BEFORE_SECONDS: Final = 60 * 60


@dataclass(frozen=True, slots=True)
class VapidKeyPair:
    """The application server's identity to every push service.

    Parsed once at construction, so a malformed key is a boot failure rather
    than a delivery failure on the first notification of the day (DI-06).
    """

    private_key: ec.EllipticCurvePrivateKey = field(repr=False)

    public_key_bytes: bytes = field(repr=False)
    """Uncompressed P-256, exactly what a browser must be given as
    `applicationServerKey`. Kept as bytes rather than re-derived per use:
    the frontend asks for it on every page that offers push."""

    subject: str
    """`mailto:` or `https:`, per RFC 8292 §2.1 — a way for a push service
    operator to reach whoever is sending. Not a secret, and the one field
    here safe to log."""

    @classmethod
    def from_base64(cls, *, private_key: str, public_key: str, subject: str) -> "VapidKeyPair":
        """Builds from the base64url forms an operator configures.

        Accepts the **raw 32-byte scalar** that every Web Push tool emits,
        not a PEM or a DER structure. That is the format `web-push
        generate-vapid-keys` prints and the format this repository's own
        operator command prints, and accepting a second one would mean
        guessing which arrived.

        Raises `ValueError` on anything that does not parse — see the class
        docstring on why that is deliberately a startup failure.
        """
        if not subject.startswith(("mailto:", "https://")):
            raise ValueError("VAPID subject must be a mailto: or https:// URI")

        scalar = b64url_decode(private_key)
        if len(scalar) != 32:
            raise ValueError(f"VAPID private key must be a 32-byte scalar, got {len(scalar)}")

        parsed = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
        derived = parsed.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)

        # The configured public key must be the one this private key
        # produces. A mismatched pair is the failure that is otherwise
        # invisible until a real browser subscribes with one key and the
        # service refuses a message signed by the other — at which point
        # every subscription created since the mistake is already worthless.
        if b64url_decode(public_key) != derived:
            raise ValueError("VAPID public key does not match the configured private key")

        return cls(private_key=parsed, public_key_bytes=derived, subject=subject)

    @property
    def public_key_base64(self) -> str:
        """The form a browser and a frontend want."""
        return b64url_encode(self.public_key_bytes)


class VapidSigner:
    """Signed assertions, one per push service origin, cached until stale.

    ## Why cached

    ES256 is cheap but not free, and a tournament round publishing to two
    hundred subscriptions would otherwise sign two hundred identical tokens.
    They *are* identical: the claims are the origin and an expiry, so every
    message to the same service within the window can carry the same one.

    Keyed by origin rather than by endpoint for the same reason — see the
    module docstring on `aud`.

    Not thread-safe and does not need to be: the delivery worker is one
    asyncio task, and a duplicated signature under a race would be correct
    anyway, merely wasted.
    """

    def __init__(self, keys: VapidKeyPair) -> None:
        self._keys = keys
        self._cache: dict[str, tuple[str, int]] = {}

    def authorization_for(self, endpoint: str, *, now: int | None = None) -> str:
        """The `Authorization` header value for one endpoint.

        `vapid t=<assertion>, k=<public key>` — RFC 8292 §3.2's single-header
        form, which every current push service accepts.
        """
        moment = int(time.time()) if now is None else now
        origin = _origin_of(endpoint)

        cached = self._cache.get(origin)
        if cached is not None and cached[1] - RENEW_BEFORE_SECONDS > moment:
            return f"vapid t={cached[0]}, k={self._keys.public_key_base64}"

        expires = moment + ASSERTION_TTL_SECONDS
        assertion = jwt.encode(
            {"aud": origin, "exp": expires, "sub": self._keys.subject},
            self._private_pem(),
            algorithm="ES256",
        )
        self._cache[origin] = (assertion, expires)
        return f"vapid t={assertion}, k={self._keys.public_key_base64}"

    def _private_pem(self) -> bytes:
        """PyJWT takes a serialised key, not a key object."""
        return self._keys.private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )


def _origin_of(endpoint: str) -> str:
    """Scheme and host, which is all `aud` may contain."""
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("push endpoint must be an absolute https URL")
    return f"{parts.scheme}://{parts.netloc}"


def generate_key_pair() -> tuple[str, str]:
    """A fresh `(private, public)` pair in the base64url forms configured.

    Used by `app/operator/push_keys.py` and by tests. **Never** called at
    startup: a platform that generated a key pair when it could not find one
    would invalidate every existing subscription on a restart, silently, and
    the symptom would be push quietly ceasing to work for everybody who had
    already enabled it.
    """
    private = ec.generate_private_key(ec.SECP256R1())
    scalar = private.private_numbers().private_value.to_bytes(32, "big")
    public = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return b64url_encode(scalar), b64url_encode(public)


__all__ = ["VapidKeyPair", "VapidSigner", "generate_key_pair"]
