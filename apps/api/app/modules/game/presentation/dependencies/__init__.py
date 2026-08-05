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

from app.api.deps import ClockDep, DbSessionDep, SettingsDep
from app.core.clock import Clock
from app.database.session import open_session
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.application.ports import ClockDeadlineStore, LiveMatchStore
from app.modules.game.application.services import (
    GameCommandService,
    GameMatchRoster,
    GameMatchSnapshot,
    LiveMoveService,
    PersistedMatchReplay,
)
from app.modules.game.application.services.match_history_service import (
    GameMatchHistory,
    GameMatchReplay,
)
from app.modules.game.application.services.match_visibility_service import (
    VisibleMatchHistory,
    VisibleMatchReplay,
)
from app.modules.game.infrastructure import RedisClockDeadlineStore, RedisLiveMatchStore
from app.modules.game.infrastructure.repositories import (
    SqlAlchemyMatchRecordRepository,
    SqlAlchemyMoveLogRepository,
)
from app.modules.game.infrastructure.repositories.match_history_repository import (
    SqlAlchemyMatchHistoryRepository,
)
from app.modules.game.public import (
    GameCommandRequest,
    GameCommandResult,
    GameCommandUseCase,
    GameEngineServices,
    MatchHistoryReader,
    MatchReplayReader,
    MatchRoster,
    MatchRosterReader,
    MatchSnapshot,
    MatchSnapshotReader,
    SubmitMoveRequest,
    SubmitMoveResult,
    SubmitMoveUseCase,
    engine_services,
)
from app.modules.users.application.services.public_profile_service import PublicProfileService
from app.modules.users.application.services.user_service import UserService
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from app.modules.users.public import PublicProfileReader
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


def get_replay_players(session: DbSessionDep, clock: ClockDep) -> PublicProfileReader:
    """How a replay resolves its two seats to handles — A64-020.5E §13.

    `users`' concrete classes, named here for the reason
    `matchmaking.presentation` names them in its own root: assembling
    another module's graph is what a composition root is for, and reaching
    into *that* root would be one module importing another's private
    presentation package — the mistake A64-010's `ClockDep` note records.

    The route holds `PublicProfileReader`, which has no way to read an
    email: the leak is unreachable rather than merely avoided.
    """
    return PublicProfileService(
        UserService(
            users=SqlAlchemyUserRepository(session),
            unit_of_work=SessionUnitOfWork(session),
            clock=clock,
        )
    )


ReplayPlayersDep = Annotated[PublicProfileReader, Depends(get_replay_players)]


class SessionScopedGameCommands:
    """`GameCommandUseCase` that opens a session per command —
    A64-020.5C-pre §5, §15.

    The same arrangement `SessionScopedLiveMoves` makes, for the identical
    reason: a WebSocket's request scope is the whole connection, so a
    service resolved through `DbSessionDep` would hold one PostgreSQL
    session per open socket for the length of a game.

    **The commit is here**, at the boundary, not inside
    `GameCommandService`. `open_session` rolls back on any path that does
    not reach the explicit `commit`, so a failed resignation propagates as
    an exception the gateway maps to a refusal — and a result can only be
    returned once the match write and the outbox event are both durable.
    That is §5's atomicity, held by the transaction rather than by ordering.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        deadlines: ClockDeadlineStore,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._deadlines = deadlines
        self._clock = clock

    async def execute(self, request: GameCommandRequest) -> GameCommandResult:
        async with open_session(self._session_factory) as session:
            service = GameCommandService(
                matches=SqlAlchemyMatchRecordRepository(session),
                deadlines=self._deadlines,
                events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
                clock=self._clock,
            )
            result = await service.execute(request)
            await session.commit()
            return result


def get_game_commands_ws(websocket: WebSocket, clock: ClockDep) -> GameCommandUseCase:
    """`game`'s participant commands, for a WebSocket route — §15.

    The **`live` Redis role** for the same reason the move path uses it:
    the deadline store is the one keyspace whose loss costs a game rather
    than a rebuild, so it must not sit on an instance configured to evict.

    No engine and no live-position cache: none of these commands touches a
    board — see `GameCommandService`.

    Typed as the port, so the gateway holds one method. It can run a
    participant command and cannot read a position or enumerate matches.
    """
    return SessionScopedGameCommands(
        session_factory=websocket.app.state.db.session_factory,
        deadlines=RedisClockDeadlineStore(websocket.app.state.redis_pools.live),
        clock=clock,
    )


WebSocketGameCommandsDep = Annotated[GameCommandUseCase, Depends(get_game_commands_ws)]


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


def get_match_history(session: DbSessionDep) -> MatchHistoryReader:
    """A player's finished matches — SPEC-REPLAY §1.

    Typed as the port, so a consumer can list finished games and cannot
    reach a lock, a write, or the move log. Request-scoped: a history page
    is one indexed read and the session ends with it.
    """
    return GameMatchHistory(SqlAlchemyMatchHistoryRepository(session))


def get_match_replay(session: DbSessionDep) -> MatchReplayReader:
    """One finished match, played back — SPEC-REPLAY §1, §4.

    Holds `ReplayEngine`, which is `game`'s and stays `game`'s: R-2 lets
    `replay` import the engine but §6 keeps the reconstruction here, so a
    consumer receives boards and never a `Match`.

    Refuses an unsupported engine version rather than approximating it —
    the refusal is `ReplayEngine`'s and is translated to the published
    `UnsupportedEngineVersion` by `GameMatchReplay`.
    """
    matches = SqlAlchemyMatchRecordRepository(session)
    return GameMatchReplay(
        replays=PersistedMatchReplay(matches=matches, moves=SqlAlchemyMoveLogRepository(session)),
        engine=engine_services().replay,
    )


def get_visible_history(session: DbSessionDep) -> VisibleMatchHistory:
    """A player's history, narrowed to what the viewer may see — §3.

    The visibility rule is applied here rather than at the route, so the
    second reader of match history inherits it instead of having to
    remember it.
    """
    return VisibleMatchHistory(get_match_history(session))


def get_visible_replay(session: DbSessionDep) -> VisibleMatchReplay:
    """One match played back, gated by the same rule.

    Holds the history reader as well as the replay reader: the gate reads
    the match's stored facts first, so a casual match a stranger asks for
    costs one row rather than a reconstruction that is then discarded.
    """
    return VisibleMatchReplay(history=get_match_history(session), replays=get_match_replay(session))


VisibleMatchHistoryDep = Annotated[VisibleMatchHistory, Depends(get_visible_history)]
VisibleMatchReplayDep = Annotated[VisibleMatchReplay, Depends(get_visible_replay)]

MatchHistoryReaderDep = Annotated[MatchHistoryReader, Depends(get_match_history)]
MatchReplayReaderDep = Annotated[MatchReplayReader, Depends(get_match_replay)]


__all__ = [
    "MatchHistoryReaderDep",
    "VisibleMatchHistoryDep",
    "VisibleMatchReplayDep",
    "get_visible_history",
    "get_visible_replay",
    "MatchReplayReaderDep",
    "get_match_history",
    "get_match_replay",
    "MatchRosterReaderDep",
    "SessionScopedSnapshots",
    "WebSocketMatchSnapshotDep",
    "get_match_snapshot_ws",
    "SessionScopedLiveMoves",
    "SessionScopedMatchRosters",
    "ReplayPlayersDep",
    "WebSocketGameCommandsDep",
    "WebSocketLiveMovesDep",
    "WebSocketMatchRosterReaderDep",
    "get_game_commands_ws",
    "get_replay_players",
    "get_live_moves_ws",
    "get_match_roster_reader",
    "get_match_roster_reader_ws",
]
