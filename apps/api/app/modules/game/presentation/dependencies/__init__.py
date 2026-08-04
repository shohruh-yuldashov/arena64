"""How `game`'s published reads are assembled — A64-016.2.

One factory today. It exists so that the gateway can resolve
`MatchRosterReader` without naming a `game` class, which is the boundary
`.importlinter` holds and the reason this package was created — see
`app/modules/game/presentation/__init__.py`.

## Why the reader holds a session *factory* and not a session

Every other repository-backed factory on this platform takes `DbSessionDep`,
because every other caller is an HTTP request and a request-scoped session is
exactly right for one. **The gateway is not a request.** `get_db_session`'s
own docstring has said so since A64-011: a session is "opened here, closed
here, never held for the life of a connection the way a WebSocket's would be
(DI-02, once the gateway entrypoint exists)".

That entrypoint now exists. A WebSocket's "request" scope is the whole
connection, so a room reader resolved through `DbSessionDep` would hold one
PostgreSQL session per open socket for as long as the player is connected —
tens of thousands of idle sessions against a pool sized for concurrent
*queries*, which is an outage rather than a slow path.

So this takes a factory and opens a session **per call**, which is the
shape `SessionScopedNotificationHandler` already uses for the same reason on
the outbox relay: the object is long-lived, the session is not. A room join
holds a connection for one primary-key lookup.

Deliberately **not** a home for `MatchAcceptanceUseCase`. `matchmaking`
already builds that one (`build_match_acceptance`), four callers reach it
through that single factory, and A64-015.5 §10 made "one construction site
per service" an asserted invariant. Moving it here would be a refactor of
working, tested wiring in a task that has no need of it — CLAUDE.md §7.6.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import ClockDep, SettingsDep
from app.core.clock import Clock
from app.database.session import open_session
from app.modules.game.application.ports import ClockDeadlineStore, LiveMatchStore
from app.modules.game.application.services import (
    GameMatchRoster,
    GameMatchSnapshot,
    LiveMoveService,
    PersistedMatchReplay,
)
from app.modules.game.infrastructure import RedisClockDeadlineStore, RedisLiveMatchStore
from app.modules.game.infrastructure.repositories import (
    SqlAlchemyMatchRecordRepository,
    SqlAlchemyMoveLogRepository,
)
from app.modules.game.public import (
    GameEngineServices,
    MatchRoster,
    MatchRosterReader,
    MatchSnapshot,
    MatchSnapshotReader,
    SubmitMoveRequest,
    SubmitMoveResult,
    SubmitMoveUseCase,
    engine_services,
)
from app.platform.outbox import OutboxEventPublisher, SqlAlchemyOutboxRepository


class SessionScopedMatchRosters:
    """`MatchRosterReader` that opens a session per read.

    Holds only long-lived things: a session factory. Nothing per-call
    survives `roster_of`, which is what makes it safe to keep one of these
    for the life of a WebSocket connection.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def roster_of(self, match_id: UUID) -> MatchRoster | None:
        """One primary-key lookup, in a session that ends with it.

        No commit: this is a read, and the `async with` releases the
        connection back to the pool on every path including the failure
        one — which is the property the whole arrangement exists for.
        """
        async with open_session(self._session_factory) as session:
            return await GameMatchRoster(SqlAlchemyMatchRecordRepository(session)).roster_of(
                match_id
            )


def get_match_roster_reader(request: Request) -> MatchRosterReader:
    """Who is in a match, behind `game`'s published read port.

    Reads the session **factory** off `app.state` rather than taking a
    resolved session, for the reason above. `Request` rather than
    `DbSessionDep` is what makes that possible — and FastAPI supplies a
    `Request` for HTTP routes and a `WebSocket` for socket ones, which is
    why `get_match_roster_reader_ws` exists beside this.

    Typed as the **port**, so a caller holding this dependency has one
    method and cannot reach `MatchRecord`, the repository, or any capability
    that could change a match. That narrowing is the whole reason the
    gateway is allowed to ask `game` anything at all.
    """
    return SessionScopedMatchRosters(request.app.state.db.session_factory)


def get_match_roster_reader_ws(websocket: WebSocket) -> MatchRosterReader:
    """The same reader, for a WebSocket route.

    A second function rather than a `Request | WebSocket` union, because
    FastAPI resolves the parameter by its *annotation*: a handler annotated
    `Request` in a WebSocket route is never called, and a union would be
    resolved to whichever member FastAPI inspects first. Two functions over
    one shared implementation is the honest spelling of "the framework has
    two scopes".
    """
    return SessionScopedMatchRosters(websocket.app.state.db.session_factory)


class SessionScopedLiveMoves:
    """`SubmitMoveUseCase` that opens a session per submission — A64-016.3.

    Same arrangement as `SessionScopedMatchRosters` above and for the same
    reason: the caller is a WebSocket, whose "request" scope is the whole
    connection, and a use case resolved through `DbSessionDep` would hold
    one PostgreSQL session per open socket for the length of a game.

    A move holds a connection for one primary-key lookup. The position
    itself is in Redis (AD-18), so the database is touched once per move
    and only to answer "who is in this match and is it active".

    Holds the engine collaborators for the **life of the process**, which is
    right and is what `engine_services()` is for: they are stateless, and
    `specs/game-engine/audit.md` §14 says building them per call is pure
    waste.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        live: LiveMatchStore,
        deadlines: ClockDeadlineStore,
        engine: GameEngineServices,
        clock: Clock,
        live_state_ttl_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._live = live
        self._deadlines = deadlines
        self._engine = engine
        self._clock = clock
        self._live_state_ttl_seconds = live_state_ttl_seconds

    async def submit(self, request: SubmitMoveRequest) -> SubmitMoveResult:
        """One move, in one transaction, acknowledged only after it commits.

        **The commit is here** — A64-016.4 §7: "return an accepted
        acknowledgement only after the durable transaction commits", and
        "if persistence fails, do not acknowledge the move as accepted".

        Both follow from the `async with`: `open_session` rolls back on any
        path that does not reach the explicit `commit`, so a failure
        propagates as an exception the gateway maps to a rejection, and a
        result can only be returned after the move row, the match write and
        the outbox events are durable.

        Placed at this boundary rather than inside `LiveMoveService` for
        the reason every service on this platform declines to commit: the
        unit of work belongs to whoever opened it, and a service that
        committed could not be composed into a larger transaction.
        """
        async with open_session(self._session_factory) as session:
            service = LiveMoveService(
                matches=SqlAlchemyMatchRecordRepository(session),
                moves=SqlAlchemyMoveLogRepository(session),
                live=self._live,
                deadlines=self._deadlines,
                events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
                generator=self._engine.generator,
                applier=self._engine.applier,
                evaluator=self._engine.terminal,
                draw_rules=self._engine.draw_rules,
                clock=self._clock,
                live_state_ttl_seconds=self._live_state_ttl_seconds,
            )
            result = await service.submit(request)
            await session.commit()
            return result


def get_live_moves_ws(
    websocket: WebSocket, settings: SettingsDep, clock: ClockDep
) -> SubmitMoveUseCase:
    """`game`'s live-play command, for a WebSocket route.

    The **`live` Redis role**, not `cache` — see `RedisLiveMatchStore` on
    why the one keyspace holding something that cannot be rebuilt must not
    sit on an instance configured to evict.

    Typed as the port, so the gateway holds one method: it can submit a
    move and cannot read a position, enumerate matches or resign one.
    """
    return SessionScopedLiveMoves(
        session_factory=websocket.app.state.db.session_factory,
        live=RedisLiveMatchStore(websocket.app.state.redis_pools.live),
        deadlines=RedisClockDeadlineStore(websocket.app.state.redis_pools.live),
        engine=engine_services(),
        clock=clock,
        live_state_ttl_seconds=settings.game.live_state_ttl_seconds,
    )


WebSocketLiveMovesDep = Annotated[SubmitMoveUseCase, Depends(get_live_moves_ws)]


class SessionScopedSnapshots:
    """`MatchSnapshotReader` that opens a session per read — A64-016.6 §1.

    Same arrangement as `SessionScopedMatchRosters` and for the same reason:
    a WebSocket's request scope is the whole connection, so a reader
    resolved through `DbSessionDep` would hold one PostgreSQL session per
    open socket for the length of a game.

    A snapshot is a replay of the durable log — one indexed read of the
    match row, one of the move log, and the engine — and the session ends
    with it.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        engine: GameEngineServices,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine
        self._clock = clock

    async def snapshot_of(self, match_id: UUID) -> MatchSnapshot | None:
        async with open_session(self._session_factory) as session:
            matches = SqlAlchemyMatchRecordRepository(session)
            return await GameMatchSnapshot(
                matches=matches,
                replays=PersistedMatchReplay(
                    matches=matches, moves=SqlAlchemyMoveLogRepository(session)
                ),
                engine=self._engine.replay,
                clock=self._clock,
            ).snapshot_of(match_id)


def get_match_snapshot_ws(websocket: WebSocket, clock: ClockDep) -> MatchSnapshotReader:
    """`game`'s live snapshot, for a WebSocket route.

    Typed as the port, so the gateway holds one method: it can ask where a
    match is and cannot enumerate matches, read a private field, or change
    anything.
    """
    return SessionScopedSnapshots(
        session_factory=websocket.app.state.db.session_factory,
        engine=engine_services(),
        clock=clock,
    )


WebSocketMatchSnapshotDep = Annotated[MatchSnapshotReader, Depends(get_match_snapshot_ws)]


MatchRosterReaderDep = Annotated[MatchRosterReader, Depends(get_match_roster_reader)]
WebSocketMatchRosterReaderDep = Annotated[MatchRosterReader, Depends(get_match_roster_reader_ws)]


__all__ = [
    "MatchRosterReaderDep",
    "SessionScopedSnapshots",
    "WebSocketMatchSnapshotDep",
    "get_match_snapshot_ws",
    "SessionScopedLiveMoves",
    "SessionScopedMatchRosters",
    "WebSocketLiveMovesDep",
    "WebSocketMatchRosterReaderDep",
    "get_live_moves_ws",
    "get_match_roster_reader",
    "get_match_roster_reader_ws",
]
