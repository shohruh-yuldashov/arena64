"""The shapes a notification *read* returns — A64-021.1 §8, §10.

Separate from `domain/record.py` because none of these is a domain concept:
a cursor is a position in a query and a page is a batch of rows, and putting
them beside the aggregate would make paging look like something a
notification knows about itself.

The same split `game.public.history` makes, and the same reason.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.modules.notifications.domain.record import NotificationRecord


@dataclass(frozen=True, slots=True)
class NotificationCursor:
    """Where a page ended: the two ordering values, never an offset.

    `(created_at, id)` descending. `OFFSET` re-scans from the start and
    shifts under concurrent inserts — and this is the one list on the
    platform where an insert *while the reader is looking at it* is the
    normal case, so a notification arriving mid-scroll would silently
    duplicate or skip a row (§8).

    `id` is the tiebreak because two notifications from one event batch
    share a millisecond often enough to matter: v7 ids are time-ordered, so
    the pair is a total order in practice as well as in principle.
    """

    created_at: datetime
    notification_id: UUID


@dataclass(frozen=True, slots=True)
class NotificationPage:
    entries: Sequence[NotificationRecord]
    next_cursor: NotificationCursor | None
    """`None` on the last page. Derived from whether a further row exists,
    so a page that is exactly `limit` long and also last does not send a
    reader back for an empty one."""


class MarkReadOutcome(StrEnum):
    """What marking one notification read actually did — A64-021.1 §9, §21.

    Three answers rather than a boolean, because the caller does something
    different with each: `NOT_FOUND` is a `404`, `MARKED` decrements the
    badge, and `ALREADY_READ` is a success that changes nothing — which is
    what makes a double click one outcome instead of two.

    A boolean would have to pick two of the three to collapse, and every
    choice of which is wrong somewhere: "owned" loses the badge answer,
    "changed" turns an already-read notification into a `404`.
    """

    MARKED = "marked"
    """It was unread and now is not."""

    ALREADY_READ = "already_read"
    """The recipient owns it and had already read it. `read_at` is unchanged."""

    NOT_FOUND = "not_found"
    """No notification with that id belongs to this recipient — which covers
    "no such notification" and "somebody else's", indistinguishably (§17)."""


__all__ = ["MarkReadOutcome", "NotificationCursor", "NotificationPage"]
