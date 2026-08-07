"""The push subscription HTTP surface — A64-021.6 §4.

## What a client may send, and what it may not

Three fields, all of them issued by the browser's own push service:
`endpoint`, `p256dh`, `auth`. That is the complete accepted surface, and
everything §4 forbids is absent by construction rather than by validation:

    user_id             the session says who this is. There is no field
                        here to fill, so there is nothing to check
    a payload           the platform composes what it sends
    a target URL        §13's click mapping is compiled into the service
                        worker; nothing on the wire can name a destination
    a VAPID key         the private one never leaves the server, and the
                        public one is served rather than accepted

`extra="forbid"` on every model, so a body carrying `user_id` is a `422`
rather than a silently ignored field — a client that thought it was setting
one should be told it was not.

## Base64url in, bytes out

The two keys arrive the way `PushSubscription.getKey()` gives them to
JavaScript: base64url, unpadded. They are decoded and length-checked here,
at the boundary, so nothing downstream handles a string that might not be
a key. A value that fails is a validation error naming the field, which is
what a client needs to fix its own serialisation.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.dto import BaseResponseDTO
from app.modules.notifications.application.services.push_subscription_service import PushStatus
from app.modules.notifications.domain.subscription import (
    MAX_ENDPOINT_LENGTH,
    PushSubscription,
)
from app.platform.push import b64url_decode

#: Base64url of 65 and 16 bytes, before padding is stripped. Bounded so an
#: oversized string is refused before it is decoded — decoding first would
#: let a client spend server memory on a value that was never a key.
_MAX_P256DH_CHARS = 128
_MAX_AUTH_CHARS = 32


class RegisterPushSubscriptionRequest(BaseModel):
    """One browser registering itself.

    Sent on enabling push and again on each app start, which is why the
    endpoint behind it is an upsert: re-registering is the normal case, not
    an error.
    """

    model_config = ConfigDict(extra="forbid")

    endpoint: Annotated[str, Field(min_length=1, max_length=MAX_ENDPOINT_LENGTH)]
    """The push service's URL for this browser.

    Validated as `https` in the domain rather than by a Pydantic URL type,
    because the rule is a security property and not a format: the delivery
    worker POSTs to whatever is stored here, so a non-https value would be
    an outbound request to an arbitrary host on a schedule.
    """

    p256dh: Annotated[str, Field(min_length=1, max_length=_MAX_P256DH_CHARS)]
    auth: Annotated[str, Field(min_length=1, max_length=_MAX_AUTH_CHARS)]

    @field_validator("p256dh", "auth")
    @classmethod
    def _is_base64url(cls, value: str) -> str:
        """Rejects anything that is not base64url, at the boundary.

        The decoded bytes are not kept — the field stays a string and the
        route decodes once. What this buys is that a malformed key produces
        a `422` naming the field, rather than a `400` from the domain that
        cannot say which of the two was wrong.
        """
        try:
            b64url_decode(value)
        except ValueError as malformed:
            raise ValueError("must be base64url") from malformed
        return value

    def decoded_p256dh(self) -> bytes:
        """The browser's public key, as the encryption takes it."""
        return b64url_decode(self.p256dh)

    def decoded_auth(self) -> bytes:
        """The browser's auth secret, as the encryption takes it."""
        return b64url_decode(self.auth)


class RemovePushSubscriptionRequest(BaseModel):
    """One browser removing itself — §22, §23.

    Carries its own endpoint rather than a subscription id, because that is
    what a browser can produce without having been told anything: it reads
    it back from `pushManager.getSubscription()`. An id would have to be
    remembered across a sign-out, which is exactly the state a sign-out
    clears.

    It is not an authorization token. The session decides whose device this
    is; a caller submitting somebody else's endpoint removes nothing and is
    told the same thing as one submitting their own.
    """

    model_config = ConfigDict(extra="forbid")

    endpoint: Annotated[str, Field(min_length=1, max_length=MAX_ENDPOINT_LENGTH)]


class PushSubscriptionResponse(BaseResponseDTO):
    """What registering answers.

    **No endpoint and no keys.** A client already has them — it sent them —
    and echoing a bearer capability back is a copy in a response body, a
    browser cache and any proxy that logs one.

    The id is safe: it names a row, is useless without the session that owns
    it, and gives a client something stable to key its own state on.
    """

    id: Annotated[str, Field(examples=["019fb9ea-0a0c-7cec-9c5f-402727c31a96"])]

    @classmethod
    def of(cls, subscription: PushSubscription) -> "PushSubscriptionResponse":
        """The one place a stored subscription becomes a response.

        A named constructor rather than `model_validate`, and that is the
        whole safety property here: `from_attributes` would copy `endpoint`,
        `p256dh` and `auth` into the response the moment somebody added them
        to this model, where this cannot serialise a field it does not name.
        """
        return cls(id=str(subscription.id))


class PushStatusResponse(BaseResponseDTO):
    """What the settings screen needs to decide what to render — §20.

    Three fields, and none of them can be turned back into a device. The
    client combines them with what only *it* knows — browser support and the
    `Notification.permission` value — to reach the state it shows, which is
    why this response deliberately does not try to be that state: the server
    cannot see a permission prompt.
    """

    available: bool
    """Whether this server can deliver a push at all. `False` means no VAPID
    key pair is configured, and the client must not offer a switch."""

    vapid_public_key: Annotated[str | None, Field(examples=["BEl62iUYgUiv..."])] = None
    """The application server key to subscribe with, or `null` when push is
    unavailable. Public by design — every browser that subscribes receives
    it — and it is the only key that ever leaves this server."""

    device_count: Annotated[int, Field(ge=0, examples=[2])]
    """How many browsers this account has registered. A count rather than a
    list: a list would have to name devices, and the only name available is
    the endpoint."""

    @classmethod
    def of(cls, status: PushStatus) -> "PushStatusResponse":
        return cls(
            available=status.available,
            vapid_public_key=status.vapid_public_key,
            device_count=status.device_count,
        )


__all__ = [
    "PushStatusResponse",
    "PushSubscriptionResponse",
    "RegisterPushSubscriptionRequest",
    "RemovePushSubscriptionRequest",
]
