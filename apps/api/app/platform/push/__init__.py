"""Outbound Web Push transport — the port, the message, and the standards.

`platform`, not a module, for the reason `platform/email` gives: a transport
that a bounded context reaches for is not that context's domain. Today
`notifications` is the only caller; the placement is what stops a second one
arriving with a second key pair and a second retry story.

Nothing here knows what a user, a notification or a subscription row is —
the `.importlinter` contract forbidding `platform` from importing a bounded
context is what keeps that true rather than a convention.

    message.py      the port, the message, the two failure types
    encoding.py     base64url without padding, which every value uses
    encryption.py   RFC 8291 — the payload a push service cannot read
    vapid.py        RFC 8292 — who this platform is, to a push service
    webpush.py      RFC 8030 — one POST, and the reading of the answer
    provider.py     whether this process can send at all
"""

from app.platform.push.encoding import b64url_decode, b64url_encode
from app.platform.push.message import (
    PermanentPushFailure,
    PushMessage,
    PushProvider,
    PushRecipient,
    TransientPushFailure,
)
from app.platform.push.provider import (
    build_push_provider,
    build_vapid_keys,
    can_deliver_push,
)
from app.platform.push.vapid import VapidKeyPair, VapidSigner, generate_key_pair
from app.platform.push.webpush import WebPushProvider

__all__ = [
    "PermanentPushFailure",
    "PushMessage",
    "PushProvider",
    "PushRecipient",
    "TransientPushFailure",
    "VapidKeyPair",
    "VapidSigner",
    "WebPushProvider",
    "b64url_decode",
    "b64url_encode",
    "build_push_provider",
    "build_vapid_keys",
    "can_deliver_push",
    "generate_key_pair",
]
