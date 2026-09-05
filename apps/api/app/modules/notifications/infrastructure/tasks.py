"""`notifications`' scheduled work — A64-021.5 §24, A64-021.6 §18.

Two handlers, one pass each of the email and push workers. A `TaskHandler` driven by
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

from app.modules.notifications.application.services.broadcast_expander import (
    BroadcastExpander,
)
from app.modules.notifications.application.services.email_delivery_service import (
    EmailDeliveryService,
)
from app.modules.notifications.application.services.push_delivery_service import (
    PushDeliveryService,
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


#: The push dispatcher key — A64-021.6.
#:
#: Its own task rather than a second pass inside the email one, and §18's
#: "do not invent a second scheduler" is satisfied by both using the same
#: `PeriodicTaskScheduler`. What separate handlers buy is that a push
#: service which has stopped answering delays pushes and not email — one
#: handler doing both would put a ten-second timeout per subscription in
#: front of every email in the batch.
PUSH_DELIVERY_TASK: Final = "notifications.push_delivery"


class PushDeliveryServiceFactory(Protocol):
    def __call__(self, session: AsyncSession) -> PushDeliveryService: ...


class NotificationPushDeliveryTask:
    """`platform.tasks.TaskHandler` — one push pass, one session."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service_factory: PushDeliveryServiceFactory,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory

    @property
    def name(self) -> str:
        return PUSH_DELIVERY_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload. The batch size is the service's, from
        `PUSH_BATCH_SIZE`."""
        async with self._session_factory() as session:
            await self._service_factory(session).deliver_once()


def push_delivery_request() -> TaskRequest:
    """The request that asks for one push pass.

    An empty payload, for the reason its email twin gives: the batch size
    and the retry schedule are configuration, and the instant is the
    handler's clock.
    """
    return TaskRequest(name=PUSH_DELIVERY_TASK, queue=MAINTENANCE_QUEUE)


#: The broadcast expander's key — A64-027A §19.
#:
#: A third handler on the same scheduler, for the same reason push is not a
#: pass inside email: a platform-wide announcement is the longest-running of
#: the three, and folding it into either would put an audience-sized write
#: in front of every email and every push in the batch.
BROADCAST_DELIVERY_TASK: Final = "notifications.broadcast_delivery"


class BroadcastExpanderFactory(Protocol):
    def __call__(self, session: AsyncSession) -> BroadcastExpander: ...


class NotificationBroadcastTask:
    """`platform.tasks.TaskHandler` — one broadcast batch, one session."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service_factory: BroadcastExpanderFactory,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory

    @property
    def name(self) -> str:
        return BROADCAST_DELIVERY_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload. One batch; the scheduler decides how often.

        Deliberately not a loop to completion — a handler that ran until a
        platform-wide broadcast finished would be a handler that ignores
        shutdown, and this is the job most likely to be running when a
        deploy happens.
        """
        async with self._session_factory() as session:
            await self._service_factory(session).run_once()


def broadcast_delivery_request() -> TaskRequest:
    """The request that asks for one broadcast batch."""
    return TaskRequest(name=BROADCAST_DELIVERY_TASK, queue=MAINTENANCE_QUEUE)


__all__ = [
    "BROADCAST_DELIVERY_TASK",
    "EMAIL_DELIVERY_TASK",
    "PUSH_DELIVERY_TASK",
    "NotificationBroadcastTask",
    "NotificationEmailDeliveryTask",
    "NotificationPushDeliveryTask",
    "broadcast_delivery_request",
    "email_delivery_request",
    "push_delivery_request",
]
