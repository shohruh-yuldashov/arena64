"""`notifications`' scheduled work — A64-021.5 §24.

One handler, one pass of the email worker. A `TaskHandler` driven by
`PeriodicTaskScheduler` rather than a bespoke `asyncio` loop, for the reason
every other periodic job on this platform is one: the scheduler owns the
interval, the cancellation and the shutdown, so a job that needs none of
those does not reimplement them.

## Why a task and not an in-process timer

AD-21's argument, and it is the same one `tournament`'s no-show sweep makes:
work held in process memory lives on one node, and a deploy takes every item
it held with it. What is owed here is a **row**, so any worker can claim it
and a restart loses nothing — which is exactly what §9 requires an email
delivery to survive.
"""

import logging
from collections.abc import Mapping
from typing import Any, Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.notifications.application.services.email_delivery_service import (
    EmailDeliveryService,
)
from app.platform.outbox import MAINTENANCE_QUEUE
from app.platform.tasks import TaskRequest

logger = logging.getLogger(__name__)

#: The dispatcher key. A constant rather than a literal at the two sites that
#: use it, because a typo in either produces a task nothing runs and nothing
#: reports — `InlineTaskDispatcher` raises for an unknown name at dispatch,
#: not at wiring.
EMAIL_DELIVERY_TASK: Final = "notifications.email_delivery"


class EmailDeliveryServiceFactory(Protocol):
    def __call__(self, session: AsyncSession) -> EmailDeliveryService: ...


class NotificationEmailDeliveryTask:
    """`platform.tasks.TaskHandler` — one email pass, one session."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service_factory: EmailDeliveryServiceFactory,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory

    @property
    def name(self) -> str:
        return EMAIL_DELIVERY_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload. The batch size is the service's, from
        `NOTIFICATION_EMAIL_BATCH_SIZE`."""
        async with self._session_factory() as session:
            await self._service_factory(session).deliver_once()


def email_delivery_request() -> TaskRequest:
    """The request that asks for one email pass.

    An empty payload: the batch size and the retry schedule are
    configuration, and the instant is the handler's clock. A request carrying
    a batch size would let a stale schedule dispatch yesterday's number at
    the one job that talks to a third party.
    """
    return TaskRequest(name=EMAIL_DELIVERY_TASK, queue=MAINTENANCE_QUEUE)


__all__ = [
    "EMAIL_DELIVERY_TASK",
    "NotificationEmailDeliveryTask",
    "email_delivery_request",
]
