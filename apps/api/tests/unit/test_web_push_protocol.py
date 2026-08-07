"""The two standards Web Push is made of — A64-021.6 §16.

**The highest-value tests in this phase**, and the reason is that both
failure modes are silent. An RFC 8291 body encrypted with the key material
in the wrong order is a valid `aes128gcm` message that a browser cannot
decrypt: the push service answers `201`, the delivery row says `sent`, the
metric says `delivered`, and nothing is displayed. A VAPID assertion signed
with the wrong key is refused by the push service with a status code, which
at least *reports* — but a mismatched key pair is only detectable by
verifying the signature against the public half a browser would hold.

So these do what no service test can: they act as **the browser**.

`test_a_browser_can_decrypt_what_this_platform_sends` derives the content
key from the wire format alone — the salt, the length prefix and the
ephemeral key are read out of the body, exactly as a user agent reads them —
and decrypts. If the info strings, their order, the delimiter or the header
framing are wrong in any way, it fails.

No network, no push service, no `httpx`. These are the pure functions.
"""

import struct

import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.platform.push import b64url_decode, generate_key_pair
from app.platform.push.encryption import encrypt
from app.platform.push.vapid import VapidKeyPair, VapidSigner

PAYLOAD = b'{"n":"019fb9ea-0a0c-7cec-9c5f-402727c31a96","t":"tournament_round_published"}'


class Browser:
    """A user agent, as far as RFC 8291 is concerned.

    Holds the key pair and the auth secret a real browser generates at
    `pushManager.subscribe()`, hands out the two public values the way a
    `PushSubscription` does, and decrypts using **only** what travels on the
    wire.
    """

    def __init__(self) -> None:
        self._private = ec.generate_private_key(ec.SECP256R1())
        self.p256dh = self._private.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )
        self.auth = b"0123456789abcdef"

    def decrypt(self, body: bytes) -> bytes:
        """RFC 8188's header, then RFC 8291's key derivation, then GCM.

        Deliberately written from the specification rather than by importing
        anything from `app.platform.push.encryption` — a test that reused
        the implementation's own constants would agree with a mistake in
        them.
        """
        salt = body[:16]
        record_size = struct.unpack("!I", body[16:20])[0]
        key_length = body[20]
        as_public = body[21 : 21 + key_length]
        ciphertext = body[21 + key_length :]

        assert record_size == len(body), "record_size must describe the single record"

        shared = self._private.exchange(
            ec.ECDH(),
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public),
        )
        prk = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self.auth,
            info=b"WebPush: info\x00" + self.p256dh + as_public,
        ).derive(shared)
        content_key = HKDF(
            algorithm=hashes.SHA256(),
            length=16,
            salt=salt,
            info=b"Content-Encoding: aes128gcm\x00",
        ).derive(prk)
        nonce = HKDF(
            algorithm=hashes.SHA256(),
            length=12,
            salt=salt,
            info=b"Content-Encoding: nonce\x00",
        ).derive(prk)

        padded = AESGCM(content_key).decrypt(nonce, ciphertext, None)
        assert padded.endswith(b"\x02"), "RFC 8188 requires the last-record delimiter"
        return padded[:-1]


class TestMessageEncryption:
    def test_a_browser_can_decrypt_what_this_platform_sends(self) -> None:
        """The whole of RFC 8291, verified from the receiving side.

        Everything this can catch fails **silently** in production: the push
        service accepts the message, the row records `sent`, and no
        notification appears. There is no log line that would show it and no
        status code that would report it — which is why this test exists and
        why it decrypts rather than asserting on the shape of the output.
        """
        browser = Browser()

        body = encrypt(plaintext=PAYLOAD, ua_public_key=browser.p256dh, auth_secret=browser.auth)

        assert browser.decrypt(body) == PAYLOAD

    def test_a_public_key_that_is_not_on_the_curve_is_refused(self) -> None:
        """The invalid-curve attack, and the check that stops it.

        A point that is not on P-256 leaks information about our ephemeral
        private key through the shared secret. `from_encoded_point` verifies
        membership, which is why the raw bytes never reach a hand-written
        parser — and why a stored key that cannot be one is refused at the
        boundary rather than producing a message nothing can read.
        """
        with pytest.raises(ValueError, match="not a valid P-256 point"):
            encrypt(plaintext=b"x", ua_public_key=b"\x04" + b"\x00" * 64, auth_secret=b"0" * 16)


class TestVapid:
    def test_the_assertion_verifies_against_the_key_a_browser_holds(self) -> None:
        """A push service checks exactly this, and refuses the message if it
        fails. Verified here with the *public* half — the value handed to
        `pushManager.subscribe()` — so the test proves the pair a browser
        committed to is the pair that signs."""
        private, public = generate_key_pair()
        signer = VapidSigner(
            VapidKeyPair.from_base64(
                private_key=private, public_key=public, subject="mailto:no-reply@arena64.gg"
            )
        )

        header = signer.authorization_for("https://fcm.googleapis.com/fcm/send/abc?x=1")

        token = header.removeprefix("vapid t=").split(",")[0]
        claims = jwt.decode(
            token,
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(public)),
            algorithms=["ES256"],
            audience="https://fcm.googleapis.com",
        )
        # The **origin**, not the endpoint. Sending the full URL puts the
        # subscription's unique path into a token, and some services refuse
        # it — a bug that presents as one vendor failing and others working.
        assert claims["aud"] == "https://fcm.googleapis.com"
        assert claims["sub"] == "mailto:no-reply@arena64.gg"

    def test_a_mismatched_key_pair_is_refused_at_construction(self) -> None:
        """The failure this catches is otherwise invisible until a real
        browser subscribes with one key and its push service refuses a
        message signed by the other — by which point every subscription
        created since the mistake is already worthless, and fixing the
        configuration does not repair them."""
        private, _ = generate_key_pair()
        _, other_public = generate_key_pair()

        with pytest.raises(ValueError, match="does not match"):
            VapidKeyPair.from_base64(
                private_key=private, public_key=other_public, subject="mailto:a@arena64.gg"
            )
