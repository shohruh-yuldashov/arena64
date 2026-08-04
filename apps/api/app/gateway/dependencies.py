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

import logging
from datetime import timedelta
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis

from app.api.deps import ClockDep, RateLimiterDep, RedisPoolsDep, SettingsDep
from app.config.settings import GatewaySettings
from app.core.clock import Clock
from app.core.rate_limiting import RateLimitRule, RateLimitScope
from app.database.redis import RedisPools
from app.gateway.bus import BusRemoteNodePublisher, GatewayBus
from app.gateway.connections import GatewayConnectionService, GatewayPolicy
from app.gateway.delivery import InMemoryLocalSockets, RoomBroadcaster
from app.gateway.event_buffer import RedisMatchEventBuffer
from app.gateway.idempotency import RedisMoveIdempotencyStore
from app.gateway.move_limits import ConnectionMoveLimiter, UnlimitedMoves
from app.gateway.moves import MoveSubmissionHandler
from app.gateway.node import resolve_node_id
from app.gateway.ports import (
    ConnectionRegistry,
    ConnectionRouter,
    LocalSocketRegistry,
    MoveRateLimiter,
    RemoteNodePublisher,
    RoomMemberStore,
)
from app.gateway.registry import RedisConnectionRegistry
from app.gateway.resume import ResumeHandler
from app.gateway.room_service import GameRoomService
from app.gateway.room_store import RedisRoomMemberStore
from app.gateway.routing import FleetConnectionRouter
from app.gateway.spectator_handler import SpectatorHandler
from app.gateway.spectator_store import RedisSpectatorStore
from app.gateway.spectators import BlockAwareSpectatorPolicy, SpectatorStore
from app.gateway.stream_bus import RedisStreamGatewayBus
from app.modules.auth.presentation.dependencies import WebSocketTicketServiceDep
from app.modules.friends.presentation.dependencies import WebSocketPairingExclusionsDep
from app.modules.game.presentation.dependencies import (
    WebSocketLiveMovesDep,
    WebSocketMatchRosterReaderDep,
    WebSocketMatchSnapshotDep,
)
from app.modules.tournament.presentation.dependencies import (
    WebSocketTournamentAttendanceDep,
)
from app.modules.users.presentation.dependencies import PresenceRecorderDep
from app.platform.metrics import MetricsRecorder, process_metrics

logger = logging.getLogger(__name__)


def get_gateway_settings(settings: SettingsDep) -> GatewaySettings:
    """The gateway's slice of configuration.

    A named dependency rather than reaching into `SettingsDep` at the call
    site, matching every other settings accessor in `app/api/deps.py` — so a
    test varying gateway bounds overrides one thing rather than the whole
    settings object.
    """
    return settings.gateway


GatewaySettingsDep = Annotated[GatewaySettings, Depends(get_gateway_settings)]


@lru_cache(maxsize=1)
def get_local_sockets() -> LocalSocketRegistry:
    """The sockets **this process** holds — A64-016.3 §10.

    Cached, so there is exactly one per process. A per-request instance
    would be a registry that never contains anything, which is the failure
    that looks like "the fan-out silently reaches nobody" — and it would
    look identical to a working single-player game.

    Process-local by nature and not a correctness mechanism: a socket is a
    file descriptor this process owns, and the fleet-wide question of
    *which* node holds a connection is `ConnectionRegistry`'s, in Redis.
    """
    return InMemoryLocalSockets()


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
LocalSocketsDep = Annotated[LocalSocketRegistry, Depends(get_local_sockets)]


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


def get_room_store(
    pools: RedisPoolsDep, clock: ClockDep, settings: GatewaySettingsDep
) -> RoomMemberStore:
    """Where room membership lives — over the `cache` role, like the
    connection registry it mirrors.

    Typed as the port, so nothing downstream can reach a Redis command or
    the reverse index that only the disconnect path should touch.
    """
    return RedisRoomMemberStore(
        pools.cache, clock=clock, progress_ttl_seconds=settings.room_ttl_seconds
    )


RoomStoreDep = Annotated[RoomMemberStore, Depends(get_room_store)]


def get_room_service(
    rosters: WebSocketMatchRosterReaderDep,
    members: RoomStoreDep,
    attendance: WebSocketTournamentAttendanceDep,
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
        # §6e. A room join is what "turned up" means for a live game, and
        # this is the only component that observes one — see
        # `tournament.public.attendance` on why the tournament is told
        # directly rather than through an event the relay would delay.
        attendance=attendance,
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


def get_gateway_bus_for(pools: RedisPools, settings: GatewaySettings) -> GatewayBus:
    """The transport frames cross between gateway processes — §9.

    `RedisStreamGatewayBus` on the **`bus`** Redis role since A64-016.5.
    AD-03 assigns that instance to pub/sub fan-out and nothing used it until
    now, which is the point: cross-node realtime traffic must not compete
    with live match positions (`live`) or with anything evictable (`cache`).

    Cached per process, because the adapter remembers which consumer groups
    it has created — a per-request instance would issue an `XGROUP CREATE`
    on every publish.
    """
    return _bus_for(pools.bus, settings.bus_max_stream_length, settings.bus_stream_ttl_seconds)


@lru_cache(maxsize=1)
def _bus_for(redis: Redis, max_stream_length: int, stream_ttl_seconds: int) -> GatewayBus:
    """One bus per process. Cached on its collaborators so a test that
    varies them gets a fresh one, and production — where all three are
    fixed — gets exactly one."""
    return RedisStreamGatewayBus(
        redis, max_stream_length=max_stream_length, stream_ttl_seconds=stream_ttl_seconds
    )


def get_gateway_bus(pools: RedisPoolsDep, settings: GatewaySettingsDep) -> GatewayBus:
    """The transport frames cross between gateway processes — A64-016.4 §9.

    Cached, so one bus serves the process. `InProcessGatewayBus` is the
    **single-node production adapter** and the test adapter both — it
    implements the contract completely and simply does not cross a process
    boundary, which is the one thing a single-node deployment does not need.

    See `get_gateway_bus_for`, which is what a caller without a request
    resolves.
    """
    return get_gateway_bus_for(pools, settings)


GatewayBusDep = Annotated[GatewayBus, Depends(get_gateway_bus)]


def get_remote_publisher(bus: GatewayBusDep) -> RemoteNodePublisher:
    """Where a remote node's share of a fan-out goes — §9.

    Over the bus since A64-016.4 §9. The publisher decides *what* to send
    from the routing plan; the bus decides *how* it travels. A64-016.3 had
    a log line here and recorded what that cost — frames silently
    undelivered on a multi-node deployment — and this is the seam that
    replaces it.

    Still one publish per node, not per connection: the grouping is the
    routing plan's and this only translates it.
    """
    return BusRemoteNodePublisher(bus)


def get_broadcaster(
    router: ConnectionRouterDep,
    sockets: LocalSocketsDep,
    publisher: Annotated[RemoteNodePublisher, Depends(get_remote_publisher)],
) -> RoomBroadcaster:
    """The room fan-out, over the routing plan — §8."""
    return RoomBroadcaster(
        router=router,
        sockets=sockets,
        publisher=publisher,
        metrics=get_gateway_metrics(),
    )


def get_move_limiter(
    request_limiter: RateLimiterDep, settings: GatewaySettingsDep
) -> MoveRateLimiter:
    """The per-connection move limit — §13.

    Over the platform's limiter rather than beside it: the sliding-window
    Lua, the fail-open posture and the key derivation are already there and
    already tested, and a second implementation would be a second place for
    "count requests in a window" to be got right.

    `UnlimitedMoves` when the switch is off, which is production code
    rather than a double — the same argument `NoPresenceProvider` makes.
    """
    if not settings.move_rate_limit_enabled:
        logger.warning("gateway_move_rate_limit_disabled", extra={"reason": "configuration"})
        return UnlimitedMoves()

    return ConnectionMoveLimiter(
        limiter=request_limiter,
        rule=RateLimitRule(
            name="gateway_move",
            scope=RateLimitScope.CONNECTION,
            limit=settings.move_rate_limit,
            window=timedelta(seconds=settings.move_rate_limit_window_seconds),
        ),
    )


@lru_cache(maxsize=1)
def _event_buffer_for(redis: Redis, max_events: int, ttl_seconds: int) -> RedisMatchEventBuffer:
    """One buffer per process, cached on its collaborators — the same shape
    the bus uses, and for the same reason: the object is cheap and the
    script registration is not."""
    return RedisMatchEventBuffer(redis, max_events=max_events, ttl_seconds=ttl_seconds)


def get_event_buffer(pools: RedisPoolsDep, settings: GatewaySettingsDep) -> RedisMatchEventBuffer:
    """The bounded replay buffer a reconnect reads — A64-016.6 §3.

    On the **`cache`** role. Losing it costs a full snapshot rather than a
    game, which is exactly the derived-and-expendable posture that instance
    is configured for — and is why it is not beside the live position.
    """
    return _event_buffer_for(
        pools.cache,
        settings.event_buffer_length,
        settings.event_buffer_ttl_seconds,
    )


EventBufferDep = Annotated[RedisMatchEventBuffer, Depends(get_event_buffer)]


def get_resume_handler(
    snapshots: WebSocketMatchSnapshotDep,
    events: EventBufferDep,
    rooms: RoomServiceDep,
) -> ResumeHandler:
    """The reconnect path — §4.

    Holds `game`'s published snapshot reader, the buffer, and the room
    service. No socket, no node identity and no bus: a resume reads
    fleet-wide state and rejoins a room, which is why §5's cross-node case
    needs nothing special.
    """
    return ResumeHandler(
        snapshots=snapshots,
        events=events,
        rooms=rooms,
        metrics=get_gateway_metrics(),
    )


ResumeHandlerDep = Annotated[ResumeHandler, Depends(get_resume_handler)]


def get_spectator_store(pools: RedisPoolsDep, clock: ClockDep) -> SpectatorStore:
    """Who is watching what — A64-016.7 §3.

    On the **`cache`** role, beside the room membership and the connection
    registry it sits next to conceptually: a subscription is a claim about a
    socket that exists right now, and losing one costs a viewer a rejoin.
    §3 forbids persisting it in PostgreSQL, and this is where that rule is
    kept rather than merely stated.
    """
    return RedisSpectatorStore(pools.cache, clock=clock)


SpectatorStoreDep = Annotated[SpectatorStore, Depends(get_spectator_store)]


def get_spectator_handler(
    snapshots: WebSocketMatchSnapshotDep,
    exclusions: WebSocketPairingExclusionsDep,
    store: SpectatorStoreDep,
    settings: GatewaySettingsDep,
) -> SpectatorHandler:
    """The `spectator.join` / `spectator.leave` handler — §2.

    `BlockAwareSpectatorPolicy` is named here, which is what a composition
    root is for: the handler holds `SpectatorEligibility` and so cannot
    reach the block graph, the match repository or anything else the policy
    consulted to reach its answer.
    """
    return SpectatorHandler(
        snapshots=snapshots,
        policy=BlockAwareSpectatorPolicy(exclusions),
        store=store,
        metrics=get_gateway_metrics(),
        subscription_ttl_seconds=settings.spectator_ttl_seconds,
    )


SpectatorHandlerDep = Annotated[SpectatorHandler, Depends(get_spectator_handler)]


def get_move_handler(
    moves: WebSocketLiveMovesDep,
    rooms: RoomServiceDep,
    broadcaster: Annotated[RoomBroadcaster, Depends(get_broadcaster)],
    buffer: EventBufferDep,
    spectators: SpectatorStoreDep,
    limiter: Annotated[MoveRateLimiter, Depends(get_move_limiter)],
    pools: RedisPoolsDep,
    settings: GatewaySettingsDep,
) -> MoveSubmissionHandler:
    """The `game.move.submit` handler, with everything it needs bound.

    Seven collaborators, which is what made A64-016.1's branching handler
    stop being the right shape and is why §1 asks for a dispatch table —
    every other handler is two lines and this one is a pipeline.

    The spectator store is the **read** side only: this handler asks who is
    watching so the fan-out can include them, and has no way to subscribe
    anybody. Joining is `SpectatorHandler`'s.
    """
    return MoveSubmissionHandler(
        moves=moves,
        rooms=rooms,
        broadcaster=broadcaster,
        buffer=buffer,
        spectators=spectators,
        idempotency=RedisMoveIdempotencyStore(pools.cache),
        limiter=limiter,
        metrics=get_gateway_metrics(),
        idempotency_ttl_seconds=settings.move_idempotency_ttl_seconds,
    )


MoveHandlerDep = Annotated[MoveSubmissionHandler, Depends(get_move_handler)]


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
    moves: MoveSubmissionHandler,
    resumes: ResumeHandler,
    spectators: SpectatorHandler,
    sockets: LocalSocketRegistry,
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
        moves=moves,
        resumes=resumes,
        spectators=spectators,
        sockets=sockets,
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
    moves: MoveHandlerDep,
    resumes: ResumeHandlerDep,
    spectators: SpectatorHandlerDep,
    sockets: LocalSocketsDep,
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
        moves=moves,
        resumes=resumes,
        spectators=spectators,
        sockets=sockets,
        presence=presence,
        clock=clock,
        node_id=node_id,
        settings=settings,
    )


GatewayServiceDep = Annotated[GatewayConnectionService, Depends(get_gateway_service)]


__all__ = [
    "ConnectionRegistryDep",
    "SpectatorHandlerDep",
    "SpectatorStoreDep",
    "get_spectator_handler",
    "get_spectator_store",
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
    "get_gateway_bus",
    "get_gateway_bus_for",
    "get_gateway_service",
    "get_gateway_settings",
    "get_node_id",
    "get_room_service",
    "get_room_store",
]
