"""Outbound Web Push — the port, the message, and nothing about a vendor.

## Why this lives in `platform`

The same rule that put `platform/email` here, for the same reason: a
transport that a bounded context reaches for is not that context's domain,
and `.importlinter`'s **platform imports no bounded context** is what keeps
it honest. Nothing in this package can reach a user, a notification or a
session — a `PushMessage` is bytes and an address, and `send` is one HTTP
round trip.

Today `notifications` is the only caller. That is not the argument for the
placement; the argument is that a second one — a security alert from `auth`,
say — must not arrive by way of a second stack, two key pairs and two retry
stories.

## Why the recipient is a value and not a row

`PushRecipient` carries the three fields a browser issued and nothing else.
It is deliberately **not** the `PushSubscription` entity: that one has an
owner, a creation time and a revocation state, all of which are the
`notifications` module's business and none of which a transport may see.
Handing the entity down would put a user id inside `platform`.

## What is secret here

All three fields. `endpoint` is a bearer capability — anybody holding it can
send a notification to that browser until it is revoked — and `p256dh` and
`auth` are the keys that make the payload readable. Every one of them is
`repr=False`, because a dataclass repr lands in tracebacks and error
reporters, and none of them is ever logged.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PushRecipient:
    """One browser's push address, exactly as the browser issued it.

    A plain frozen dataclass rather than a Pydantic model, for the reason
    `EmailMessage` gives: nothing here crosses the wire to a client, and a
    Pydantic model is one keystroke from being a `response_model`. These
    three fields are the credential that lets anybody notify that browser.
    """

    endpoint: str = field(repr=False)
    """The push service's URL for this browser. Vendor-specific by design —
    Mozilla, Google and Apple each run their own — and treated as opaque:
    nothing in this platform parses it, branches on its host, or decides
    anything from it. A URL that arrived from a browser is a URL, not a
    routing hint."""

    p256dh: bytes = field(repr=False)
    """The browser's public key, uncompressed P-256 (65 bytes, `0x04`
    prefixed). Half of the ECDH exchange that derives the content key."""

    auth: bytes = field(repr=False)
    """The browser's authentication secret (16 bytes). Salts the key
    derivation, so possession of the public key alone does not let a push
    service read the payload — which is the whole point of RFC 8291."""


@dataclass(frozen=True, slots=True)
class PushMessage:
    """One notification, ready to hand to a transport.

    ## Why the payload is `bytes` and not a dict

    Because encryption operates on octets and the *caller* owns the
    serialisation. A transport that took a dict would be deciding the wire
    format for every future consumer, and a transport that took a string
    would be deciding the encoding. Neither is a transport's decision.

    It also keeps the size limit meaningful: a push service rejects a
    payload over roughly 4 KB *after* encryption, and only the caller can
    shorten a message it composed.
    """

    recipient: PushRecipient

    payload: bytes = field(repr=False)
    """The plaintext, already serialised. `repr=False` because a push
    payload names a notification and a type, and a repr lands in
    tracebacks."""

    ttl_seconds: int = 0
    """How long the push service may hold this if the browser is offline.

    Zero means *deliver now or drop it*, and is deliberately **not** the
    default this platform uses — `notifications` sets a real window. It is
    the default here because a transport should not invent a retention
    policy for messages it knows nothing about.
    """

    urgency: str = "normal"
    """RFC 8030's hint, which a push service uses to decide whether to wake
    a sleeping device. `normal` for everything this platform sends: `high`
    is for calls and alarms, and a tournament round is neither."""


class PermanentPushFailure(Exception):
    """A rejection that will recur, whoever retries it — A64-021.6 §17.

    Two distinct causes share this type, and both are permanent for the same
    reason: the *subscription* is finished.

        gone            `404`/`410`. The browser unsubscribed, the profile
                        was cleared, or the push service expired it
        malformed       an endpoint or key set the service will not accept

    The caller's response is to stop retrying and revoke the subscription —
    never to retry with a different key, because there is no different key.

    **Here rather than in `notifications`**, for the reason its email twin
    gives: it is the transport's classification of its own outcome, and a
    module that imported it from a bounded context would invert the
    dependency this package exists to keep pointing one way.
    """


class TransientPushFailure(Exception):
    """A rejection worth asking about again — A64-021.6 §17.

    A timeout, a connection reset, a `429`, a `5xx`. The subscription is
    presumed fine and the push service is not; the caller's response is
    backoff, not revocation.

    Separate from `PermanentPushFailure` rather than a flag on it, because
    the two lead to opposite actions and a boolean on one exception type is
    exactly the branch that gets read backwards at three in the morning.
    """


class PushProvider(Protocol):
    """Where a push message goes.

    One method, async for the reason every transport port on this platform
    is: a synchronous `send` in the delivery worker blocks the event loop for
    a network round trip, and the worker's entire job is network round trips.

    ## Why it returns `None`

    There is nothing truthful to return. A push service accepting a message
    means it accepted it — not that the browser was awake, not that the
    person saw it, and not that the service will still hold it in an hour.
    Unlike email there is not even a provider-side message id to hand an
    operator, because the protocol defines none.

    So the outcome is the *absence of an exception*, and the two exceptions
    above carry the only distinction a caller can act on.
    """

    async def send(self, message: PushMessage) -> None:
        """Delivers one message.

        Raises `PermanentPushFailure` when the subscription is finished and
        `TransientPushFailure` when the service is. Any other exception is a
        defect in this platform rather than a delivery outcome, and callers
        should let it propagate.
        """
        ...


__all__ = [
    "PermanentPushFailure",
    "PushMessage",
    "PushProvider",
    "PushRecipient",
    "TransientPushFailure",
]
