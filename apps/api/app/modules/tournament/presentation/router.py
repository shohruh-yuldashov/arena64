"""A tournament's public reads — SPEC-TOURNAMENT §7, A64-019.6 §9–§13.

Four endpoints, all read-only. Thin, like every other router on this
platform: each handler resolves a service, converts a path parameter and
maps a value to a response — no SQL, no placement arithmetic and no
visibility rule of its own.

    GET /tournaments/{id}             the detail page
    GET /tournaments/{id}/bracket     rounds, nodes and attempts
    GET /tournaments/{id}/standings   the immutable final result
    GET /players/{id}/tournaments     one player's participation

## Public, and what that means here

§7: tournaments, brackets and results are visible to everybody. **Visible**
is not the same as unauthenticated — every route on this platform outside
`/health` is behind a session, and these are no exception. What "public"
buys is that no viewer is narrower than another: there is no owner check,
no friends-only variant and no configurable privacy, and a private
tournament is deferred with user-created ones.

So a tournament that does not exist is a `404`, and there is no case in
which a real one answers `403` — the resource is either there for everybody
or absent for everybody, which is the one shape §7's "404, never 403" rule
cannot be got wrong in.

## Nothing here writes

Completion is automatic (§14): the final winner finishes the tournament
through the advancement flow. There is deliberately no `POST /complete`,
because an endpoint that could finish a bracket is one an operator could
finish an *unfinished* bracket with, and OQ-1 leaves moderation to the
Administration epic.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, status

from app.api.openapi import error_response
from app.api.responses import build_response
from app.core.exceptions import NotFoundError
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.tournament.presentation.dependencies import TournamentResultsDep
from app.modules.tournament.presentation.schemas.results import (
    BracketResponse,
    PlayerTournamentsResponse,
    StandingsResponse,
    TournamentResponse,
    decode_cursor,
)

#: Two prefixes, so each path reads as the resource it is about. A player's
#: tournaments hang off `/players` — beside their match history, which is
#: the list a client renders next to it.
tournaments_router = APIRouter(prefix="/tournaments", tags=["tournaments"])
player_tournaments_router = APIRouter(prefix="/players", tags=["tournaments"])

#: The page bounds for a player's history. Stated here because the route is
#: the only thing that bounds it — the repository takes whatever it is given.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


@tournaments_router.get(
    "/{tournament_id}",
    response_model=ApiResponse[TournamentResponse],
    status_code=status.HTTP_200_OK,
    summary="One tournament",
    responses=error_response(404, "No such tournament"),
)
async def tournament_detail(
    user: CurrentUser,
    results: TournamentResultsDep,
    tournament_id: Annotated[UUID, Path(description="Which tournament to read.")],
) -> ApiResponse[TournamentResponse]:
    """A tournament's public detail — §9.

    Everything a lobby or a detail page renders: the configuration, how full
    it is, which round is being played, and the three lifecycle instants.
    """
    summary = await results.summary(tournament_id)
    if summary is None:
        raise NotFoundError("That tournament does not exist.")
    return build_response(TournamentResponse.of(summary))


@tournaments_router.get(
    "/{tournament_id}/bracket",
    response_model=ApiResponse[BracketResponse],
    status_code=status.HTTP_200_OK,
    summary="A tournament's bracket",
    responses=error_response(404, "No such tournament"),
)
async def tournament_bracket(
    user: CurrentUser,
    results: TournamentResultsDep,
    tournament_id: Annotated[UUID, Path(description="Which tournament's bracket to read.")],
) -> ApiResponse[BracketResponse]:
    """Every round, node and attempt — §10.

    Read whole rather than paged: a field is at most 128 (T-2), so a bracket
    is at most 127 nodes and rendering one needs all of them.

    A tournament whose bracket has not been materialised answers with an
    empty round list rather than a `404` — the tournament exists, and "no
    bracket yet" is a state a client renders rather than an error.
    """
    if await results.summary(tournament_id) is None:
        raise NotFoundError("That tournament does not exist.")
    return build_response(BracketResponse.of(await results.bracket(tournament_id)))


@tournaments_router.get(
    "/{tournament_id}/standings",
    response_model=ApiResponse[StandingsResponse],
    status_code=status.HTTP_200_OK,
    summary="A tournament's final standings",
    responses=error_response(404, "No such tournament"),
)
async def tournament_standings(
    user: CurrentUser,
    results: TournamentResultsDep,
    tournament_id: Annotated[UUID, Path(description="Which tournament's results to read.")],
) -> ApiResponse[StandingsResponse]:
    """The immutable final placement — §11.

    Ordered by rank, then seed, then player id. **Empty while the tournament
    is being played**: standings are materialised once, when it completes
    (§6f), and nothing here derives a partial one — a placement that changed
    between two reads would not be a result.
    """
    if await results.summary(tournament_id) is None:
        raise NotFoundError("That tournament does not exist.")
    standings = await results.standings(tournament_id)
    return build_response(StandingsResponse.of(tournament_id, standings))


@player_tournaments_router.get(
    "/{player_id}/tournaments",
    response_model=ApiResponse[PlayerTournamentsResponse],
    status_code=status.HTTP_200_OK,
    summary="A player's tournaments",
    responses=error_response(400, "The pagination cursor is not valid"),
)
async def player_tournaments(
    user: CurrentUser,
    results: TournamentResultsDep,
    player_id: Annotated[UUID, Path(description="Whose participation to read.")],
    after: Annotated[
        str | None, Query(description="An opaque cursor from a previous page.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
) -> ApiResponse[PlayerTournamentsResponse]:
    """Completed and active participation, newest first — §12.

    **Keyset**, never `OFFSET`: a history is unbounded, and an offset scan
    costs more with every page and shifts when a row is inserted mid-scan.

    Public, like everything else here: `player_id` says whose history to
    read and there is no narrowing by viewer, because a tournament entry is
    a public competitive record. `final_rank` and `final_status` are `null`
    while a tournament is running, which is what tells a client to render
    "in progress" rather than a placing.
    """
    page = await results.player_history(
        player_id,
        after=decode_cursor(after) if after else None,
        limit=limit,
    )
    return build_response(PlayerTournamentsResponse.of(page))


__all__ = ["player_tournaments_router", "tournaments_router"]
