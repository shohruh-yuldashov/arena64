"""The gateway's composition root — A64-016.1.

Two objects to build, and the interesting part is where their collaborators
come from:

    WebSocketTicketService   `auth.presentation.dependencies` — `auth.public`
                             deliberately publishes nothing that can mint or
                             inspect a credential, and says so: "every other
                             module consumes the *result* of authentication
                             through the dependencies in
                             `auth.presentation.dependencies`, and never the
                             machinery"
    PresenceRecorder         `users.public` — the published write port, whose
                             own docstring names this task as its caller
    ConnectionRegistry       built here. It is the gateway's own storage, not
                             a module's, so nothing else has a claim on it

That is the whole of what the gateway reaches for, and `.importlinter`'s
`gateway-reaches-modules-through-public` contract holds it to exactly that:
no module's `domain`, `application` or `infrastructure` is importable from
here, which is R-7 ("the gateway contains no domain logic") expressed as an
import rule rather than as a hope.

## Why `Depends` at all on a WebSocket route

FastAPI resolves dependencies for `@router.websocket` exactly as it does for
HTTP, so the gateway gets the same per-request session and settings wiring
every other route has, and DI-01 stays true — `Depends` used at the routing
layer to hand an already-resolved service to a handler.

The one thing it does *not* get is an exception handler: a WebSocket route
that raises produces a closed socket and a traceback, not a response
envelope. Which is why `GatewayConnectionService.run` never raises and why
this file resolves everything before the handshake is accepted.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, RedisPoolsDep, SettingsDep
from app.config.settings import GatewaySettings
from app.core.clock import Clock
from app.gateway.connections import GatewayConnectionService, GatewayPolicy
from app.gateway.ports import ConnectionRegistry
from app.gateway.registry import RedisConnectionRegistry
from app.modules.auth.presentation.dependencies import WebSocketTicketServiceDep
from app.modules.users.presentation.dependencies import PresenceRecorderDep
from app.platform.metrics import MetricsRecorder, process_metrics


def get_gateway_settings(settings: SettingsDep) -> GatewaySettings:
    """The gateway's slice of configuration.

    A named dependency rather than reaching into `SettingsDep` at the call
    site, matching every other settings accessor in `app/api/deps.py` — so a
    test varying gateway bounds overrides one thing rather than the whole
    settings object.
    """
    return settings.gateway


GatewaySettingsDep = Annotated[GatewaySettings, Depends(get_gateway_settings)]


def get_connection_registry(pools: RedisPoolsDep, clock: ClockDep) -> ConnectionRegistry:
    """The fleet-wide connection registry, over the `cache` role.

    **No kill switch**, unlike presence and the friends cache. Those degrade
    to a working platform with a feature missing; a gateway with no registry
    cannot answer "does this player have another connection", so presence
    would flap on every closed tab and A64-016.2 would have nowhere to route
    a move. A switch whose off position is "broken" is not a switch.

    Typed as the port, so nothing downstream can reach a Redis command.
    """
    return RedisConnectionRegistry(pools.cache, clock=clock)


ConnectionRegistryDep = Annotated[ConnectionRegistry, Depends(get_connection_registry)]


def get_gateway_metrics() -> MetricsRecorder:
    """The process-wide recorder — A64-015.6 §10.

    The same accumulator the composition root and every HTTP route count
    into, so `MetricsFlushTask` drains the gateway's counters too. A second
    recorder here would be counters nothing ever emits, which is the precise
    defect that task found and closed.
    """
    return process_metrics()


def build_gateway_service(
    *,
    tickets: WebSocketTicketServiceDep,
    registry: ConnectionRegistryDep,
    presence: PresenceRecorderDep,
    clock: Clock,
    settings: GatewaySettings,
) -> GatewayConnectionService:
    """The lifecycle, with its four collaborators and its bounds.

    Takes plain arguments rather than resolving `Depends` itself, for the
    reason `build_queue_service` does: the day a gateway worker or a test
    harness needs the identical graph without a request, it calls this. A
    factory reachable only through `Depends` is one the background path
    assembles its own copy of, and the two drift on the first collaborator
    either gains.
    """
    return GatewayConnectionService(
        tickets=tickets,
        registry=registry,
        presence=presence,
        metrics=get_gateway_metrics(),
        clock=clock,
        policy=GatewayPolicy(
            connection_ttl_seconds=settings.connection_ttl_seconds,
            heartbeat_timeout_seconds=settings.heartbeat_timeout_seconds,
            max_frame_bytes=settings.max_frame_bytes,
        ),
    )


def get_gateway_service(
    tickets: WebSocketTicketServiceDep,
    registry: ConnectionRegistryDep,
    presence: PresenceRecorderDep,
    clock: ClockDep,
    settings: GatewaySettingsDep,
) -> GatewayConnectionService:
    """The `Depends` wrapper the route annotates."""
    return build_gateway_service(
        tickets=tickets,
        registry=registry,
        presence=presence,
        clock=clock,
        settings=settings,
    )


GatewayServiceDep = Annotated[GatewayConnectionService, Depends(get_gateway_service)]


__all__ = [
    "ConnectionRegistryDep",
    "GatewayServiceDep",
    "GatewaySettingsDep",
    "build_gateway_service",
    "get_connection_registry",
    "get_gateway_metrics",
    "get_gateway_service",
    "get_gateway_settings",
]
