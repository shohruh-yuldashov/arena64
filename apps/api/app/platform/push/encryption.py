"""RFC 8291 message encryption for Web Push — A64-021.6 §16.

Roughly eighty lines implementing one standard, and the reason it is here
rather than in a package is recorded in `pyproject.toml`: the alternative
brings four dependencies and a synchronous HTTP client to obtain this file.

## What the standard actually requires

A push service is an untrusted intermediary. It routes the message and must
not be able to read it, so the payload is encrypted to a key only the
browser holds — and the browser handed that key over at subscription time
(`p256dh`), together with a shared secret (`auth`) that keeps a push service
holding the public key from deriving the same thing.

The scheme is `aes128gcm` (RFC 8188), keyed as RFC 8291 §3.4 specifies:

    ecdh_secret = ECDH(our ephemeral private key, the browser's public key)

    PRK       = HKDF(salt=auth_secret, ikm=ecdh_secret,
                     info="WebPush: info" || 0x00 || ua_public || as_public)
    CEK       = HKDF(salt=random_salt, ikm=PRK,
                     info="Content-Encoding: aes128gcm" || 0x00)[:16]
    nonce     = HKDF(salt=random_salt, ikm=PRK,
                     info="Content-Encoding: nonce" || 0x00)[:12]

    body      = salt(16) || record_size(4) || len(as_public)(1) || as_public
                         || AES128GCM(CEK, nonce, plaintext || 0x02)

Two details are easy to get wrong and both are silent — the message is
simply never displayed:

  * the **order** of the two public keys in the PRK info string is
    user-agent first, application server second. Reversing it produces a
    valid-looking key that decrypts to nothing;
  * the plaintext is padded with a single `0x02` **delimiter** before
    encryption. RFC 8188 uses `0x02` for the last record and `0x01` for any
    other; this platform never sends more than one record.

## One record, always

`record_size` is set to the whole body, so every message is a single record.
Multi-record framing exists for streams; a push payload is capped near 4 KB
by every push service, and this platform's payloads are two identifiers.

## Ephemeral keys

A fresh key pair per message, which the standard requires — reusing one
across messages would let a push service correlate them, and would make one
key compromise readable history rather than one readable message.
"""

import os
import struct
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

#: The curve every Web Push implementation uses. RFC 8291 §2 permits no
#: other, so this is not a configuration point.
CURVE: Final = ec.SECP256R1()

#: Salt length, content key length and nonce length, all fixed by RFC 8188.
SALT_BYTES: Final = 16
KEY_BYTES: Final = 16
NONCE_BYTES: Final = 12

#: The last (and here only) record's padding delimiter — RFC 8188 §2.
LAST_RECORD_DELIMITER: Final = b"\x02"

#: The largest plaintext a push service will reliably carry.
#:
#: Every major service caps the *encrypted* body at 4096 octets, and the
#: framing above costs 86 of them (16 salt + 4 length + 1 + 65 key) plus 17
#: for the delimiter and GCM tag. Refusing early gives the caller a defect
#: it can fix, where a push service's refusal arrives as an opaque 400 per
#: subscription.
MAX_PLAINTEXT_BYTES: Final = 4096 - SALT_BYTES - 4 - 1 - 65 - 1 - 16


def encrypt(*, plaintext: bytes, ua_public_key: bytes, auth_secret: bytes) -> bytes:
    """One `aes128gcm` body, ready to POST.

    Raises `ValueError` for a key set that cannot be used or a payload that
    cannot fit — both are defects at the call site rather than delivery
    outcomes, and both are worth failing loudly on: a subscription whose
    public key does not parse will never work, and a caller composing an
    oversized payload has a bug in the payload, not in the browser.
    """
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise ValueError(
            f"push payload is {len(plaintext)} bytes; the limit is {MAX_PLAINTEXT_BYTES}"
        )
    if len(auth_secret) != 16:
        raise ValueError(f"auth secret must be 16 bytes, got {len(auth_secret)}")

    # `from_encoded_point` validates that the point is actually on the curve.
    # An attacker-chosen point that is not would leak information about our
    # ephemeral private key through the shared secret — the invalid-curve
    # attack — and this is the check that prevents it. It is why the raw
    # bytes are never fed to a hand-written parser.
    try:
        browser_key = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, ua_public_key)
    except ValueError as malformed:
        raise ValueError("subscription public key is not a valid P-256 point") from malformed

    ours = ec.generate_private_key(CURVE)
    as_public = ours.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    shared = ours.exchange(ec.ECDH(), browser_key)

    # RFC 8291 §3.4. The order of the two keys is user-agent first — see the
    # module docstring on why reversing it fails silently.
    prk = _hkdf(
        salt=auth_secret,
        key_material=shared,
        info=b"WebPush: info\x00" + ua_public_key + as_public,
        length=32,
    )
    salt = os.urandom(SALT_BYTES)
    content_key = _hkdf(
        salt=salt,
        key_material=prk,
        info=b"Content-Encoding: aes128gcm\x00",
        length=KEY_BYTES,
    )
    nonce = _hkdf(
        salt=salt,
        key_material=prk,
        info=b"Content-Encoding: nonce\x00",
        length=NONCE_BYTES,
    )

    ciphertext = AESGCM(content_key).encrypt(nonce, plaintext + LAST_RECORD_DELIMITER, None)

    # RFC 8188 §2's header, then the single record. `record_size` describes
    # the whole body because there is exactly one record.
    body = salt + struct.pack("!I", len(ciphertext) + 86) + bytes([len(as_public)]) + as_public
    return body + ciphertext


def _hkdf(*, salt: bytes, key_material: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256, the only derivation RFC 8291 uses."""
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(key_material)


__all__ = ["MAX_PLAINTEXT_BYTES", "encrypt"]
