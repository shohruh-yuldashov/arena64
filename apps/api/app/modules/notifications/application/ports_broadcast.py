"""What a broadcast needs from the outside — A64-027A §19.

A separate module from `ports.py` for the reason `analytics` split
`ports_read.py` out: these are the seams of one capability, and a single
`ports.py` holding every protocol in the module becomes a file nobody can
read to find out what one service depends on.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.notifications.domain.broadcast import Broadcast, BroadcastStatus


class BroadcastRepository(Protocol):
    """Where broadcasts live."""

    async def create(self, broadcast: Broadcast) -> Broadcast:
        """Stores it, or returns the one already stored under its key.

        **Returns rather than raises on a duplicate.** A double-submitted
        composer is not an error to report to an administrator — it is the
        same intention expressed twice, and the correct answer is the
        broadcast they already created. §18: the console must not be able
        to produce two announcements from one form.
        """
        ...

    async def claim_next(self, *, now: datetime) -> Broadcast | None:
        """The oldest unfinished broadcast, marked `SENDING`.

        Claimed with `FOR UPDATE SKIP LOCKED`, so two workers running
        against one database take two different broadcasts rather than
        racing on the same one. `None` when there is no work.
        """
        ...

    async def record_progress(
        self,
        broadcast_id: UUID,
        *,
        cursor: UUID | None,
        delivered: int,
        audience_size: int | None,
    ) -> None:
        """Advances the keyset and the delivered count.

        `delivered` is an increment, not a total: the caller knows how many
        rows its batch wrote and does not know what a concurrent retry may
        have written, so the addition belongs in the statement.
        """
        ...

    async def finish(
        self,
        broadcast_id: UUID,
        *,
        status: BroadcastStatus,
        at: datetime,
        failure_reason: str | None = None,
    ) -> None:
        """Marks it `COMPLETED` or `FAILED`."""
        ...

    async def get(self, broadcast_id: UUID) -> Broadcast | None: ...

    async def page(self, *, limit: int, before: datetime | None) -> Sequence[Broadcast]:
        """The history, newest first, keyset-paged on `created_at`."""
        ...


__all__ = ["BroadcastRepository"]
