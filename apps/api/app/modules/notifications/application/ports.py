"""`notifications`' ports — AD-06.

One published-to-us contract per collaborator, and one of our own:

    PresenceWriter   what the producer needs of `users`' presence: read the
                     current record, write the new one. Narrower than
                     `PresenceService`, which is what makes the transition
                     check the only thing this module can do with presence
    NotificationSink where a rendered notification goes

Everything else this module consumes is already a published port somewhere
else — `friends.public.PresenceAudience`, `profiles.public.ProfileRenderer`,
`platform.outbox.EventPublisher` — and re-declaring them here would be a
second copy of a contract, which is the thing ports exist to prevent.

## Why `PresenceWriter` is declared here and not imported

`users.public` publishes `PresenceProvider` (read) and `PresenceRecorder`
(write), and this module needs *both* plus the guarantee that they address
the same store. `PresenceService` already composes exactly that, but it is
an application service rather than a published port — and publishing it
would hand every future consumer the ability to mark anybody online.

So the consumer declares the shape it needs (AD-06: the port belongs to the
layer that needs it), and the composition root satisfies it with
`PresenceService`. The seam costs one Protocol and buys a producer that
cannot be handed anything wider.
"""

from typing import Protocol
from uuid import UUID

from app.modules.notifications.domain.notification import SocialNotification
from app.modules.users.public import DeviceType, Presence


class PresenceWriter(Protocol):
    """Read-then-write access to one player's presence.

    Satisfied by `users.application.services.PresenceService`.

    The **read** is what makes edge detection possible, and it is the reason
    this is one port rather than two: a producer holding a reader and a
    writer that happened to address different stores would detect
    transitions against one and record them in another, which would look
    like working code and would emit an event on every refresh.
    """

    async def presence_of(self, player_id: UUID) -> Presence | None:
        """This player's current record, or `None` if there is none.

        `None` is "no observation" — never seen, or the window lapsed — and
        is treated as offline by the transition check. It is deliberately not
        distinguished from an explicit offline record: both mean the player
        is not here, and a transition into "here" is the same edge either
        way.
        """
        ...

    async def mark_online(
        self,
        player_id: UUID,
        *,
        session_id: UUID | None = None,
        device_type: DeviceType | None = None,
    ) -> None:
        """Records that the player is present and restarts their window.

        Never raises (`PresenceRecorder`'s contract): a sign-in must not fail
        because Redis was briefly unreachable.
        """
        ...

    async def mark_offline(self, player_id: UUID) -> None:
        """Records that the player has gone. Never raises."""
        ...


class NotificationSink(Protocol):
    """Where a rendered notification goes.

    The seam A64-013.7 stops at. Every transport the brief excludes —
    WebSocket, push, email, mobile — is an implementation of this, and the
    one that exists today writes a log line.

    **Batch-first**, like `EventHandler`: the dispatcher resolves an audience
    and renders once, producing many notifications at a time, and a singular
    method would be called in a loop by every implementation.

    A sink **may raise**. Unlike the presence recorder, whose failures are
    cosmetic and swallowed, a delivery that failed is a delivery the platform
    should retry — so the exception propagates to the dispatcher, which turns
    it into a recorded per-event failure and a backoff. Swallowing it here
    would mark the event published and lose the notification silently.
    """

    async def deliver(self, notifications: list[SocialNotification]) -> None:
        """Delivers a batch. An empty batch is a legal no-op."""
        ...
