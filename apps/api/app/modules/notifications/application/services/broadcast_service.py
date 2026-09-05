"""Creating and reading broadcasts — A64-027A §14, §18, §20.

The admin-facing half. It does **not** deliver anything: `create` writes one
row and returns, which is what §19 requires — an HTTP request that looped
over an audience would hold a connection open for the length of a delivery
and would lose the whole send if the client disconnected.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports_broadcast import BroadcastRepository
from app.modules.notifications.domain.broadcast import (
    Broadcast,
    BroadcastAudience,
    BroadcastChannel,
    BroadcastStatus,
)
from app.modules.users.public import NotificationAudienceDirectory


@dataclass(frozen=True, slots=True)
class BroadcastRequest:
    """What an administrator composed, already validated at the boundary."""

    title: str
    body: str
    locale: str
    audience: BroadcastAudience
    idempotency_key: str
    recipients: tuple[UUID, ...] = ()


class BroadcastService:
    def __init__(
        self,
        *,
        repository: BroadcastRepository,
        audience: NotificationAudienceDirectory,
        clock: Clock,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._repository = repository
        self._audience = audience
        self._clock = clock
        self._unit_of_work = unit_of_work

    async def preview_audience_size(self, audience: BroadcastAudience) -> int:
        """How many accounts this audience currently resolves to.

        Server-side, because §14 forbids a recipient count the console
        invented: it is the number an administrator reads immediately before
        deciding to send to everybody, and a frontend estimate would be the
        most trusted wrong number in the product.
        """
        if audience is BroadcastAudience.ALL_PLAYERS:
            return await self._audience.count_eligible()
        return 0

    async def create(self, request: BroadcastRequest, *, created_by: UUID) -> Broadcast:
        """Queues one broadcast. Idempotent on `(created_by, key)`.

        The audience is **not** resolved here. Counting every eligible
        account inside the admin request would make the response time a
        function of the platform's size, and the count would be stale by the
        time the worker ran anyway — so the expander counts, once, as its
        first act.
        """
        broadcast = Broadcast(
            id=uuid4(),
            title=request.title.strip(),
            body=request.body.strip(),
            locale=request.locale,
            audience=request.audience,
            channel=BroadcastChannel.IN_APP,
            status=BroadcastStatus.QUEUED,
            created_by=created_by,
            created_at=self._clock.now(),
            idempotency_key=request.idempotency_key,
            recipients=request.recipients,
        )
        async with self._unit_of_work:
            stored = await self._repository.create(broadcast)
            # `SessionUnitOfWork` rolls back on an exception and commits
            # **nothing** on its own — repositories.md §5.1: "exiting the
            # scope without an explicit commit rolls back". Without this the
            # endpoint answers `202` with a broadcast id and stores no row.
            await self._unit_of_work.commit()
            return stored

    async def get(self, broadcast_id: UUID) -> Broadcast | None:
        return await self._repository.get(broadcast_id)

    async def history(self, *, limit: int, before: datetime | None) -> Sequence[Broadcast]:
        return await self._repository.page(limit=limit, before=before)


__all__ = ["BroadcastRequest", "BroadcastService"]
