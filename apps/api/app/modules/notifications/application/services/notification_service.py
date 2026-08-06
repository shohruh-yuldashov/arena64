"""`NotificationService` — reading a recipient's notifications, and marking
them read. A64-021.1 §9, §10, §15.

Thin on purpose: every method is one repository call plus the transaction
boundary. There is no business rule here beyond ownership, and ownership is
enforced by the *shape* of the port rather than by a check in this file —
`NotificationRepository` has no method that can reach a row without a
recipient, so this service could not read somebody else's notification if it
tried (§30).

## What this service deliberately cannot do

There is **no create**. A notification is produced by a source event through
`DurableNotificationWriter`, never by a request, and §15 forbids an endpoint
that mints arbitrary notifications. Nothing here writes a row that a player
did not already earn by something happening to them.

## Read state and the source aggregate

Marking a notification read touches exactly one row in
`notifications.notification`. It does not resolve a friend request, does not
touch a friendship, and does not tell anybody. §9's "read state must not
modify source aggregates", and the reason it is easy to honour: this module
has no port that could reach one.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.core.exceptions import NotFoundError
from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports import NotificationRepository
from app.modules.notifications.application.read_models import (
    MarkReadOutcome,
    NotificationCursor,
    NotificationPage,
)

logger = logging.getLogger(__name__)

#: The page sizes the API offers. Here rather than in the router so that one
#: place bounds a page, whatever calls it — the same arrangement
#: `game`'s match history makes.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
"""Lower than the platform's usual 100. A notification row is taller than a
history row and a client renders every one of them; fifty is already more
than a viewport, and the list a player wants is the recent one."""


class NotificationService:
    """One recipient's notifications, read and marked."""

    def __init__(
        self,
        *,
        notifications: NotificationRepository,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._notifications = notifications
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def list_for(
        self,
        recipient_id: UUID,
        *,
        after: NotificationCursor | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> NotificationPage:
        """One page of this recipient's notifications, newest first."""
        return await self._notifications.list_for(
            recipient_id, after=after, limit=min(limit, MAX_PAGE_SIZE)
        )

    async def unread_count(self, recipient_id: UUID) -> int:
        """How many are unread. One query, no rows — §10."""
        return await self._notifications.count_unread(recipient_id)

    async def mark_read(self, notification_id: UUID, *, recipient_id: UUID) -> bool:
        """Marks one notification read. `True` if this call is what changed it.

        Raises `NotFoundError` when this recipient owns no notification with
        that id — which covers both "no such notification" and "somebody
        else's", deliberately indistinguishably (§17). A `403` for the second
        would confirm the row exists, which is enough to probe for other
        people's notifications one id at a time.

        Idempotent: marking an already-read notification succeeds and leaves
        its original `read_at` alone, so a double click is one outcome rather
        than a rewritten history.

        `read_at` is **server time** (§9) — the client's clock is not
        evidence of when anything was read.
        """
        async with self._unit_of_work:
            outcome = await self._notifications.mark_read(
                notification_id, recipient_id=recipient_id, at=self._clock.now()
            )
            # Committed on **every** path, including the refusal, and raised
            # outside the scope. A `NOT_FOUND` means the `UPDATE` matched no
            # row, so there is nothing to undo — and leaving the scope
            # without committing would roll back the caller's whole
            # transaction to discard a statement that changed nothing. A
            # refusal must not have side effects on work it did not do.
            await self._unit_of_work.commit()

        if outcome is MarkReadOutcome.NOT_FOUND:
            raise NotFoundError("No such notification.")

        return outcome is MarkReadOutcome.MARKED

    async def mark_all_read(self, recipient_id: UUID) -> int:
        """Marks every unread notification read. Returns how many changed.

        One statement, bounded by what is unread (§9). A recipient with
        nothing unread commits an empty transaction and returns zero, which
        is a legal no-op rather than an error — a client cannot know the
        count was already zero when the button was pressed.
        """
        async with self._unit_of_work:
            changed = await self._notifications.mark_all_read(recipient_id, at=self._clock.now())
            await self._unit_of_work.commit()

        logger.info(
            "notifications_marked_read",
            # The recipient is the platform's standard log field; the
            # notifications themselves are social facts and never logged.
            extra={"recipient_id": str(recipient_id), "count": changed},
        )
        return changed


__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "NotificationService"]
