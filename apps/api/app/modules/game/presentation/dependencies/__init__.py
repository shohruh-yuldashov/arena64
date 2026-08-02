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

from app.database.session import open_session
from app.modules.game.application.services import GameMatchRoster
from app.modules.game.infrastructure.repositories import SqlAlchemyMatchRecordRepository
from app.modules.game.public import MatchRoster, MatchRosterReader


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


MatchRosterReaderDep = Annotated[MatchRosterReader, Depends(get_match_roster_reader)]
WebSocketMatchRosterReaderDep = Annotated[MatchRosterReader, Depends(get_match_roster_reader_ws)]


__all__ = [
    "MatchRosterReaderDep",
    "SessionScopedMatchRosters",
    "WebSocketMatchRosterReaderDep",
    "get_match_roster_reader",
    "get_match_roster_reader_ws",
]
