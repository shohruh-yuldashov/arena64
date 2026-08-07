"""Registering and removing browsers — A64-021.6 §3, §7, §22, §23.

Three use cases and one rule that governs all of them: **the caller's
identity comes from the session, never from the request.** There is no
`user_id` parameter on any public method that a transport could fill from a
body, which is §3's requirement expressed as a signature rather than a check.

## What "the browser owns the endpoint" means here

A client submits three values its own push service issued, and this service
believes exactly that much: those bytes came from that browser. It does not
believe any claim about whose browser it is — the session says that — and it
does not believe an endpoint identifies a device it has seen before.

That is why re-registration is an upsert that **replaces ownership** rather
than a check that refuses a conflict. A browser saying "this endpoint is
mine now" is the only signal available that a previous binding is stale, and
refusing it would leave a shared laptop permanently bound to whoever used it
first — which is the leak §23 exists to prevent, not a protection against it.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from app.core.clock import Clock
from app.core.exceptions import ValidationError
from app.core.identifiers import generate_uuid7
from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports import PushSubscriptionRepository
from app.modules.notifications.domain.preference import ChannelAvailability, DeliveryChannel
from app.modules.notifications.domain.subscription import PushSubscription, is_well_formed

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PushStatus:
    """What a client needs to render the push section of settings — §20.

    Deliberately **not** a subscription. It answers "can this platform push
    at all" and "how many browsers are registered", and neither answer can
    be turned back into an endpoint or a key.

    The count is there so a person can tell "I have three devices signed up"
    from "I have none", which is the distinction §20 requires the UI to
    make and which a boolean would flatten.
    """

    available: bool
    """Whether this process holds a VAPID key pair and can deliver."""

    vapid_public_key: str | None
    """The application server key a browser must subscribe with, or `None`
    when push is unconfigured. Public by design — it is handed to every
    browser — and it is the one value in this module safe to serve."""

    device_count: int
    """How many live browsers this account has registered."""


class PushSubscriptionService:
    """Browsers, registered and removed."""

    def __init__(
        self,
        *,
        subscriptions: PushSubscriptionRepository,
        unit_of_work: UnitOfWork,
        clock: Clock,
        availability: ChannelAvailability,
        vapid_public_key: str | None,
    ) -> None:
        self._subscriptions = subscriptions
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._availability = availability
        self._vapid_public_key = vapid_public_key

    async def register(
        self, user_id: UUID, *, endpoint: str, p256dh: bytes, auth: bytes
    ) -> PushSubscription:
        """Registers, re-registers or takes over one browser's subscription.

        `user_id` comes from the session at the call site and is the only
        way it can arrive — see the module docstring.

        Refuses when this process cannot deliver: storing a capability that
        nothing will ever use is a row that looks like a working device and
        is not, and the client would show a person an enabled switch behind
        an unconfigured platform (§6).
        """
        if not self._availability.can_deliver(DeliveryChannel.PUSH):
            raise ValidationError("Push notifications are not available on this server.")

        # Domain rules, checked before anything is written — an endpoint
        # that is not https would otherwise become an outbound request to an
        # arbitrary host every time the worker ran (§25).
        malformed = is_well_formed(endpoint=endpoint, p256dh=p256dh, auth=auth)
        if malformed is not None:
            raise ValidationError(f"That push subscription is not usable: {malformed}.")

        now = self._clock.now()
        # Generated here rather than inline, so the log below can say
        # whether this **took over** an existing row: the repository returns
        # the stored id, which is the pre-existing one on a conflict.
        proposed = generate_uuid7()
        async with self._unit_of_work:
            stored = await self._subscriptions.register(
                PushSubscription(
                    id=proposed,
                    user_id=user_id,
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth,
                    created_at=now,
                    updated_at=now,
                    last_seen_at=now,
                )
            )
            await self._unit_of_work.commit()

        # Ids and a count. **No endpoint and no key**, here or anywhere —
        # §25, and an endpoint in a log line is a capability in a log
        # aggregator that outlives the subscription.
        logger.info(
            "push_subscription_registered",
            extra={"subscription_id": str(stored.id), "existing": stored.id != proposed},
        )
        return stored

    async def remove(self, user_id: UUID, *, endpoint: str) -> bool:
        """Removes the calling browser's own subscription — §22, §23.

        Scoped to the owner, so a caller cannot remove somebody else's
        device by guessing a URL. Returns whether anything was live.

        **Idempotent, and answers the same for an endpoint that was never
        theirs.** Telling the two apart would answer "does this endpoint
        belong to another account", which is an enumeration oracle for a
        value that is a bearer capability.

        This is the path a sign-out takes (§23). It is deliberately separate
        from turning the *preference* off: a person who mutes push keeps
        their devices registered and re-enables in one click, where somebody
        signing out must leave nothing behind that could be pushed to.
        """
        now = self._clock.now()
        async with self._unit_of_work:
            removed = await self._subscriptions.revoke_by_endpoint(
                endpoint, user_id=user_id, at=now
            )
            await self._unit_of_work.commit()
        return removed

    async def status(self, user_id: UUID) -> PushStatus:
        """What the settings screen needs — §20.

        Reports availability even when it is `False`, with a `None` key, so
        a client can say *"push is not available on this server"* rather
        than showing a switch that fails on tap.
        """
        available = self._availability.can_deliver(DeliveryChannel.PUSH)
        devices = await self._subscriptions.live_for(user_id)
        return PushStatus(
            available=available,
            vapid_public_key=self._vapid_public_key if available else None,
            device_count=len(devices),
        )


__all__ = ["PushStatus", "PushSubscriptionService"]
