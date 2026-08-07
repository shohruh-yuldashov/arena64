"""A browser's push subscription — A64-021.6 §2, §3, §23.

Framework-free. One entity and the rules about who owns it, when it dies,
and what makes two of them the same.

## Identity is the endpoint, not the row

A browser can only tell this platform one thing about itself: the endpoint
its push service issued. It has no device id, no stable installation id, and
nothing that survives clearing site data. So `endpoint` is the natural key,
and the surrogate `id` exists only so that a delivery row can point at
something short.

That has a consequence worth stating plainly, because §2 warns about it:
**an endpoint is not a public identity**. It is a bearer capability — anyone
holding it can push to that browser, subject to VAPID — so it is never
returned to a client, never logged, and never used as a lookup key that a
caller supplies.

## Re-subscription, and why ownership is replaced rather than rejected

A browser re-subscribes routinely: after a permission reset, after a service
worker update, after a push service rotates its endpoints. Sometimes it
produces the same endpoint and sometimes a new one, and this platform cannot
tell which case it is in.

So a subscription arriving for an endpoint that already exists **takes it
over**. Not "is rejected as a duplicate" — that would leave a browser unable
to fix itself — and not "creates a second row", which would push twice to
one browser.

The security question this raises is answered in §23 and by
`SubscriptionOwnershipReplaced`: if the existing row belongs to a different
user, the takeover is the *point*. Two people sharing a laptop must not
inherit each other's notifications, and the browser telling us "this
endpoint is now mine" is exactly the signal that the previous binding is
stale.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

#: Field size limits, enforced at the boundary — §4.
#:
#: An endpoint is a URL a push service chose and they are long: FCM's run to
#: about 200 characters and Mozilla's to about 160. 2048 is generous enough
#: that no real service is refused and small enough that the column cannot
#: be used as storage.
MAX_ENDPOINT_LENGTH: Final = 2048

#: Uncompressed P-256 (65 bytes) and the auth secret (16), both fixed by
#: RFC 8291. Exact rather than maximum, because a value of any other length
#: cannot encrypt anything and storing it would produce a subscription that
#: fails on every delivery forever.
P256DH_BYTES: Final = 65
AUTH_BYTES: Final = 16


@dataclass(frozen=True, slots=True)
class PushSubscription:
    """One browser, subscribed for one account.

    Frozen, like every entity in this module. Changes are new rows written
    through the repository, so nothing can mutate a subscription in memory
    and forget to save it.
    """

    id: UUID
    user_id: UUID

    endpoint: str
    """The push service's URL for this browser. Never returned to a client
    and never logged — see the module docstring."""

    p256dh: bytes
    """The browser's public key. Half of the ECDH exchange."""

    auth: bytes
    """The browser's authentication secret."""

    created_at: datetime
    updated_at: datetime

    last_seen_at: datetime
    """When this browser last confirmed the subscription is still its own.

    Written on every re-registration, which a client does on each start.
    It is the only signal available for "is this device still real" — a
    push service tells us nothing until a delivery fails — and it is what a
    future retention sweep would key on.
    """

    revoked_at: datetime | None = None
    """When this stopped being deliverable, or `None` while it is live.

    Set rather than deleted, and the reason is the one every delivery record
    on this platform gives: an operator asking *"why did this device stop
    getting notifications"* gets an answer, where a deleted row looks
    identical to one that never existed.

    Two things set it: a push service answering `404`/`410` (the browser is
    gone), and the person signing out on that browser (§23).
    """

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None


def is_well_formed(*, endpoint: str, p256dh: bytes, auth: bytes) -> str | None:
    """Why this key set cannot be stored, or `None` when it can — §4.

    Returns a **reason** rather than raising or returning a bool, because
    the caller turns it into a validation error the client can act on and a
    boolean would make every message "invalid subscription".

    Checked here rather than only in a Pydantic schema because these are
    domain rules — a 65-byte key is RFC 8291's requirement, not a transport
    detail — and because the same rules apply to a subscription arriving
    from anywhere, including a future migration or an operator command.
    """
    if not endpoint.startswith("https://"):
        # `http://` and every other scheme. A push service is always https,
        # and accepting anything else would let a stored endpoint become an
        # outbound request to an arbitrary host — the delivery worker POSTs
        # to whatever is in this column.
        return "endpoint must be an absolute https URL"
    if len(endpoint) > MAX_ENDPOINT_LENGTH:
        return f"endpoint must be at most {MAX_ENDPOINT_LENGTH} characters"
    if len(p256dh) != P256DH_BYTES:
        return f"p256dh must be {P256DH_BYTES} bytes"
    if len(auth) != AUTH_BYTES:
        return f"auth must be {AUTH_BYTES} bytes"
    return None


__all__ = [
    "AUTH_BYTES",
    "MAX_ENDPOINT_LENGTH",
    "P256DH_BYTES",
    "PushSubscription",
    "is_well_formed",
]
