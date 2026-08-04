"""The periodic task that drains this node's bus stream — A64-016.8.

`platform.tasks`, not Celery, for the reason AD-17 gives and every other
scheduled job on this platform follows: the handler is a plain object with a
name and a coroutine, driven by the inline dispatcher today and by a worker
tomorrow without changing.

## Why this handler takes no session factory

Every other task here opens a PostgreSQL session because its work is a query.
This one's work is Redis and a dictionary of file descriptors — it reads the
node's own stream and writes to sockets this process holds — so a session
would be a connection checked out of the pool on every tick and returned
unused.

That is also why the forwarder is built **once** at composition rather than
per run: it closes over the process's `LocalSocketRegistry`, and a forwarder
rebuilt per tick over a fresh registry would deliver to an empty map, which
looks exactly like a working system with no remote traffic.
"""

import logging
from collections.abc import Mapping
from typing import Any, Final

from app.gateway.forwarding import GatewayForwarder
from app.platform.tasks import TaskRequest

logger = logging.getLogger(__name__)

#: The name `PeriodicTaskScheduler` dispatches and this handler answers to.
GATEWAY_FORWARDING_TASK: Final = "gateway.bus.forward"

#: The queue this work is routed to once queues exist (AD-20).
#:
#: **`realtime`**, the same pool the clock adjudicator uses and for the same
#: reason: a forwarding pass that is a second late is an opponent's move a
#: second late, and sharing a pool with a retention sweep would let a slow
#: prune delay every cross-node frame on the fleet.
REALTIME_QUEUE: Final = "realtime"


def forwarding_request() -> TaskRequest:
    """The request that asks for one forwarding pass."""
    return TaskRequest(name=GATEWAY_FORWARDING_TASK, queue=REALTIME_QUEUE)


class GatewayForwardingTask:
    """`platform.tasks.TaskHandler` — one pass over this node's stream."""

    def __init__(self, forwarder: GatewayForwarder) -> None:
        self._forwarder = forwarder

    @property
    def name(self) -> str:
        return GATEWAY_FORWARDING_TASK

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Ignores the payload — see `forwarding_request`.

        Does not catch: `forward_once` records its own failures and never
        raises, so a `try` here would be a second swallow with nothing left
        to swallow.
        """
        await self._forwarder.forward_once()


__all__ = [
    "GATEWAY_FORWARDING_TASK",
    "REALTIME_QUEUE",
    "GatewayForwardingTask",
    "forwarding_request",
]
