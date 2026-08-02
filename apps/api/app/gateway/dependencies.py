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
from app.gateway.node import resolve_node_id
from app.gateway.ports import ConnectionRegistry, ConnectionRouter, RoomMemberStore
from app.gateway.registry import RedisConnectionRegistry
from app.gateway.room_service import GameRoomService
from app.gateway.room_store import RedisRoomMemberStore
from app.gateway.routing import FleetConnectionRouter
from app.modules.auth.presentation.dependencies import WebSocketTicketServiceDep
from app.modules.game.presentation.dependencies import WebSocketMatchRosterReaderDep
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


def get_node_id(settings: GatewaySettingsDep) -> str:
    """This process's node identifier — A64-016.2 §3.

    A dependency rather than a module global so that the *resolution* is
    visible in the graph, and cached beneath (`node.resolve_node_id`) so
    that resolving it per request costs a dictionary lookup and yields the
    same string every time. §3's "stable for the process lifetime" is held
    by the cache, not by this function being called once.
    """
    return resolve_node_id(settings)


NodeIdDep = Annotated[str, Depends(get_node_id)]


def get_room_store(pools: RedisPoolsDep, clock: ClockDep) -> RoomMemberStore:
    """Where room membership lives — over the `cache` role, like the
    connection registry it mirrors.

    Typed as the port, so nothing downstream can reach a Redis command or
    the reverse index that only the disconnect path should touch.
    """
    return RedisRoomMemberStore(pools.cache, clock=clock)


RoomStoreDep = Annotated[RoomMemberStore, Depends(get_room_store)]


def get_room_service(
    rosters: WebSocketMatchRosterReaderDep,
    members: RoomStoreDep,
    clock: ClockDep,
    settings: GatewaySettingsDep,
) -> GameRoomService:
    """The membership rule, over `game`'s published roster.

    `game`'s own factory, resolved through its presentation layer — the
    same arrangement `auth`'s ticket service reaches the gateway by, and the
    reason `.importlinter`'s gateway contract passes: this file names no
    `game` internal.

    The **WebSocket** variant, and the distinction is not cosmetic. A
    session resolved through `DbSessionDep` lives for the "request", which
    for a socket is the whole connection — one idle PostgreSQL session per
    open tab, for the length of a game. That reader opens a session per
    read instead; see `game.presentation.dependencies`.
    """
    return GameRoomService(
        rosters=rosters,
        members=members,
        metrics=get_gateway_metrics(),
        clock=clock,
        room_ttl_seconds=settings.room_ttl_seconds,
    )


RoomServiceDep = Annotated[GameRoomService, Depends(get_room_service)]


def get_connection_router(registry: ConnectionRegistryDep, node_id: NodeIdDep) -> ConnectionRouter:
    """The cross-node routing seam — A64-016.2 §9.

    Wired now, called by nothing. That is deliberate rather than an
    oversight: §9 asks for the seam "required by future tasks" and forbids
    the transport, and a port that is constructed and typed is one
    A64-016.3 resolves instead of designs.
    """
    return FleetConnectionRouter(registry=registry, node_id=node_id, metrics=get_gateway_metrics())


ConnectionRouterDep = Annotated[ConnectionRouter, Depends(get_connection_router)]


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
    rooms: GameRoomService,
    presence: PresenceRecorderDep,
    clock: Clock,
    node_id: str,
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
        rooms=rooms,
        presence=presence,
        metrics=get_gateway_metrics(),
        clock=clock,
        policy=GatewayPolicy(
            connection_ttl_seconds=settings.connection_ttl_seconds,
            heartbeat_timeout_seconds=settings.heartbeat_timeout_seconds,
            max_frame_bytes=settings.max_frame_bytes,
            node_id=node_id,
        ),
    )


def get_gateway_service(
    tickets: WebSocketTicketServiceDep,
    registry: ConnectionRegistryDep,
    rooms: RoomServiceDep,
    presence: PresenceRecorderDep,
    clock: ClockDep,
    node_id: NodeIdDep,
    settings: GatewaySettingsDep,
) -> GatewayConnectionService:
    """The `Depends` wrapper the route annotates."""
    return build_gateway_service(
        tickets=tickets,
        registry=registry,
        rooms=rooms,
        presence=presence,
        clock=clock,
        node_id=node_id,
        settings=settings,
    )


GatewayServiceDep = Annotated[GatewayConnectionService, Depends(get_gateway_service)]


__all__ = [
    "ConnectionRegistryDep",
    "ConnectionRouterDep",
    "GatewayServiceDep",
    "GatewaySettingsDep",
    "NodeIdDep",
    "RoomServiceDep",
    "RoomStoreDep",
    "build_gateway_service",
    "get_connection_registry",
    "get_gateway_metrics",
    "get_connection_router",
    "get_gateway_service",
    "get_gateway_settings",
    "get_node_id",
    "get_room_service",
    "get_room_store",
]
