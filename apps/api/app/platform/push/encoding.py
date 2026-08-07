"""base64url without padding — the encoding every Web Push value uses.

Browsers hand `p256dh` and `auth` to JavaScript as base64url with the `=`
padding stripped, VAPID keys are distributed the same way, and RFC 7515
requires it of a JWT. Python's `urlsafe_b64decode` refuses unpadded input,
so every caller would otherwise re-derive the same `+ "=" * (-len % 4)`.

One place, so the padding arithmetic is written once and the decoder is
strict in the same way everywhere.
"""

import base64


def b64url_encode(raw: bytes) -> str:
    """Unpadded base64url, the form a browser and an operator expect."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    """Decodes with or without padding.

    Raises `ValueError` on anything that is not base64url — including the
    standard alphabet's `+` and `/`, which `validate=True` rejects rather
    than silently discarding. A subscription key that arrived in the wrong
    alphabet is malformed input, and accepting it would store bytes that can
    never encrypt anything.
    """
    padded = value + "=" * (-len(value) % 4)
    try:
        # `b64decode(validate=True)` rather than `urlsafe_b64decode`, which
        # takes the lenient path and **discards** characters outside the
        # alphabet — so `"++"` would decode to nothing instead of failing,
        # and a malformed key would be stored as short bytes that encrypt
        # nothing. `altchars` supplies the url-safe alphabet that the strict
        # decoder does not have a variant for.
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as malformed:
        raise ValueError("value is not base64url") from malformed


__all__ = ["b64url_decode", "b64url_encode"]
