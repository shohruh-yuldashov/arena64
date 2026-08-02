"""`FleetConnectionRouter` — where a message would have to go. A64-016.2 §9.

The seam and nothing behind it. §9 asks for "a routing port that can answer
which node owns each recipient connection, which local connections can
receive directly, and which remote nodes require forwarding", and forbids the
broker, the remote delivery and the Pub/Sub.

## Why compute the split now, with no transport to use it

Because the split is where the **decision** lives, and the transport is
where the plumbing lives. Building both at once means the first thing that
needs cross-node delivery has to get two unfamiliar things right in one
change; building this now means A64-016.3 writes a publisher against a value
that already exists, is already exercised, and has already had its edge cases
argued — a player with no connections, a player with tabs on two nodes, a
route whose node is this one.

It is also the honest place for the cost. Resolving a plan is one Redis read
per recipient, and a room has two: that is the load the transport will add
regardless of how it delivers, and having it in the code now means it shows
up in a profile before it shows up in an incident.

## What it deliberately does not do

No delivery, no fan-out, no Pub/Sub, no retry, no ordering guarantee. It
returns a `RoutingPlan` and the caller does something with it — today,
nothing does, which is why there is no publisher here to be half-written.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from app.gateway.metrics import ROUTE_RESOLUTIONS, RouteLocality
from app.gateway.ports import ConnectionRegistry, ConnectionRoute, RoutingPlan
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)


class FleetConnectionRouter:
    """Divides recipients into "this process" and "somebody else's".

    Holds the registry and this node's identity, which between them are the
    whole of what the question needs: the registry says where every
    connection is, and the node id is what makes "here" meaningful.
    """

    def __init__(
        self, *, registry: ConnectionRegistry, node_id: str, metrics: MetricsRecorder
    ) -> None:
        self._registry = registry
        self._node_id = node_id
        self._metrics = metrics

    async def plan_for(self, player_ids: Sequence[UUID]) -> RoutingPlan:
        """Every live connection of every named player, partitioned.

        **Deduplicates the recipients**, because a caller fanning out to a
        room's participants may legitimately name the same player twice —
        a room whose two seats resolved to one account would otherwise
        deliver everything twice — and because the same player named twice
        is one Redis read rather than two.

        A player with no live connections contributes nothing and is not an
        error: they are offline, which is the ordinary state of half the
        people a message concerns.
        """
        local: list[ConnectionRoute] = []
        remote: dict[str, list[ConnectionRoute]] = {}

        for player_id in dict.fromkeys(player_ids):
            for route in await self._registry.routes_for(player_id):
                if route.node_id == self._node_id:
                    local.append(route)
                else:
                    remote.setdefault(route.node_id, []).append(route)

        # Counted by **locality**, never by node (§11): one series per node
        # is a cardinality that grows with the fleet, and a node identifier
        # in a metric is internal topology in a system with broader read
        # access than the registry. The ratio is the operational question —
        # a rising remote share is what says a transport is now actually
        # needed.
        self._metrics.increment(
            ROUTE_RESOLUTIONS, labels={"locality": RouteLocality.LOCAL}, by=len(local)
        )
        self._metrics.increment(
            ROUTE_RESOLUTIONS,
            labels={"locality": RouteLocality.REMOTE},
            by=sum(len(routes) for routes in remote.values()),
        )

        return RoutingPlan(
            local=tuple(local),
            remote={node: tuple(routes) for node, routes in remote.items()},
        )


__all__ = ["FleetConnectionRouter"]
