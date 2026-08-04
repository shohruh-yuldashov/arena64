"""The FastAPI `Depends` bridge for `rating` — dependency-injection.md DI-01.

Two factories, and the split is the *capability* rather than the graph:

    get_player_rating_repository   loads an aggregate so it can be mutated
                                   and saved. The rating-application path
    get_rating_reader              returns snapshots that cannot be. Every
                                   consumer outside this module

`matchmaking` and `profiles` are handed the reader, so neither holds a
method that can move a rating. R-4's one-way chain is a constructor argument
rather than a rule to remember.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import DbSessionDep
from app.database.session import open_session
from app.modules.rating.application.ports import PlayerRatingRepository
from app.modules.rating.domain.keys import RatingKey
from app.modules.rating.infrastructure.repositories.leaderboard_repository import (
    SqlAlchemyLeaderboardReader,
)
from app.modules.rating.infrastructure.repositories.player_rating_repository import (
    SqlAlchemyPlayerRatingRepository,
    SqlAlchemyRatingReader,
)
from app.modules.rating.public import LeaderboardReader, RatingReader, RatingSnapshot


def get_player_rating_repository(session: DbSessionDep) -> PlayerRatingRepository:
    """The aggregate's storage, over this request's session."""
    return SqlAlchemyPlayerRatingRepository(session)


def get_leaderboard_reader(session: DbSessionDep) -> LeaderboardReader:
    """The standings read — A64-017.4.

    A **query**, not a projection: there is no cache to warm, nothing to
    rebuild and nothing to invalidate, because the relation it reads is the
    one a rating update writes. See `rating.public.leaderboard`.
    """
    return SqlAlchemyLeaderboardReader(session)


LeaderboardReaderDep = Annotated[LeaderboardReader, Depends(get_leaderboard_reader)]


def get_rating_reader(session: DbSessionDep) -> RatingReader:
    """`rating`'s published read, typed as the port.

    Typed as `RatingReader` rather than as the concrete class, so a consumer
    resolving this dependency cannot reach `save` by accident — the
    published surface is what they get, and it has no write.
    """
    return SqlAlchemyRatingReader(session)


RatingReaderDep = Annotated[RatingReader, Depends(get_rating_reader)]
PlayerRatingRepositoryDep = Annotated[PlayerRatingRepository, Depends(get_player_rating_repository)]


class SessionScopedRatingReader:
    """`RatingReader` that opens a session per read.

    The arrangement `game`'s `SessionScopedSnapshots` uses, and for the same
    reason: the caller is a **background scan** (`matchmaking`'s pairing
    task) rather than a request, so a reader bound to a request-scoped
    session would have none to bind to.

    A rating read is one indexed lookup and the session ends with it.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def rating_for(self, player_id, *, key: RatingKey) -> RatingSnapshot:  # type: ignore[no-untyped-def]
        async with open_session(self._session_factory) as session:
            return await SqlAlchemyRatingReader(session).rating_for(player_id, key=key)

    async def ratings_for(self, player_ids, *, key: RatingKey):  # type: ignore[no-untyped-def]
        async with open_session(self._session_factory) as session:
            return await SqlAlchemyRatingReader(session).ratings_for(player_ids, key=key)


__all__ = [
    "LeaderboardReaderDep",
    "PlayerRatingRepositoryDep",
    "RatingReaderDep",
    "SessionScopedRatingReader",
    "get_leaderboard_reader",
    "get_player_rating_repository",
    "get_rating_reader",
]
