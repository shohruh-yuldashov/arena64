"""`PreferenceDeliveryPolicy` — the preference check, at delivery time.
A64-021.3 §10, §12.

`SocialNotificationDispatcher` has re-read the audience and the privacy gate
at delivery since A64-013.7, for a reason its docstring states plainly:
*"a player blocked between an accept and its delivery must not be told"*.
A preference is the same kind of fact. Somebody who mutes a category between
the friend request and the relay tick that carries it has muted it, and the
only way that holds is if the question is asked **here** rather than
answered when the event was written.

## It prevents creation; it does not hide

§10: *"Do not write then hide notifications."* A muted category produces no
row, so there is no unread count to move, no realtime frame to send, and
nothing for a later change of mind to reveal. A row written and filtered on
read would be a record the player never consented to, sitting in a table
they cannot see — and the first reporting query would find it.

## One query per batch, not per recipient

The port takes a sequence and answers a set. A friend request has one
recipient today; a published tournament round has as many as it has
entrants, and a policy shaped for a single lookup would make that an N+1
that nobody notices until the first large bracket (§11).

## What it deliberately is not

Not a cache. §11 says measure before adding Redis, and the measurement is
that a delivery already costs an audience read, a profile render and an
insert — one more indexed read against a table that is empty for most
players is not the thing to optimise first.
"""

import logging
from collections.abc import Sequence

from app.modules.notifications.application.ports import (
    DeliveryRequest,
    NotificationPreferenceRepository,
)
from app.modules.notifications.domain.preference import DeliveryChannel

logger = logging.getLogger(__name__)


class PreferenceDeliveryPolicy:
    """Answers "may this be delivered" from stored overrides and defaults.

    Holds the repository and nothing else — no clock, no session of its own,
    no notion of what a notification *is*. It is asked about a recipient and
    a category, which is all a preference is keyed on.
    """

    def __init__(self, *, preferences: NotificationPreferenceRepository) -> None:
        self._preferences = preferences

    async def permitted(
        self, requests: Sequence[DeliveryRequest], *, channel: DeliveryChannel
    ) -> frozenset[DeliveryRequest]:
        """The subset that may be delivered. An empty input is a legal no-op."""
        if not requests:
            return frozenset()

        allowed = await self._preferences.permitted(requests, channel=channel)

        suppressed = len(set(requests)) - len(allowed)
        if suppressed:
            # Counted, never named. That a notification was suppressed is an
            # operational fact; *whose* preference suppressed it is the
            # personal choice this log must not publish.
            logger.info(
                "notification_suppressed_by_preference",
                extra={"channel": channel.value, "suppressed": suppressed},
            )
        return allowed


__all__ = ["PreferenceDeliveryPolicy"]
