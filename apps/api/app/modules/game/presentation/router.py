"""`GET /players/{id}/matches` and `GET /matches/{id}/replay` — SPEC-REPLAY.

Thin, as A64-018.3 §5 requires. Each handler resolves a service, converts a
path parameter, and maps a value to a response — no SQL, no visibility
arithmetic and no participant check of its own. The rule lives in
`game.application.services.match_visibility_service`, so the second reader
of match history inherits it.

## Two endpoints, two very different costs

`/matches` lists stored facts: one indexed page read, no reconstruction.
`/replay` replays a whole game through the rules. They are separate routes
because they are separate costs, and because SPEC-REPLAY §4 needs them to
diverge — a match played under an unsupported engine version answers the
first and refuses the second.

## Why a hidden match is `404` and never `403`

SPEC-REPLAY §3. A `403` confirms the match is real, which is enough to
enumerate match ids and learn who is playing casually with whom. So a
casual match a stranger requests is answered exactly as an id that was
never issued — same status, same body, same path through this file.

`UnsupportedEngineVersion` is different and is allowed to be specific: it is
raised only *after* the viewer has been found entitled to see the match, so
it discloses nothing they could not already read in their own history.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, status

from app.api.openapi import error_response
from app.api.responses import build_response
from app.core.exceptions import NotFoundError
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.game.presentation.dependencies import (
    ReplayPlayersDep,
    VisibleMatchHistoryDep,
    VisibleMatchReplayDep,
)
from app.modules.game.presentation.schemas.history import (
    MatchHistoryResponse,
    MatchReplayResponse,
    decode_cursor,
)

#: Two prefixes, so each path reads as the resource it is about. A single
#: `/game` prefix would put a player's history under a noun the client has
#: no other reason to know.
history_router = APIRouter(prefix="/players", tags=["match history"])
replay_router = APIRouter(prefix="/matches", tags=["match history"])

#: The default and maximum page sizes are the repository's; the route only
#: passes the request through, so there is one place that bounds a page.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


@history_router.get(
    "/{player_id}/matches",
    response_model=ApiResponse[MatchHistoryResponse],
    status_code=status.HTTP_200_OK,
    summary="A player's finished matches",
    responses=error_response(400, "The pagination cursor is not valid"),
)
async def player_match_history(
    user: CurrentUser,
    history: VisibleMatchHistoryDep,
    player_id: Annotated[UUID, Path(description="Whose history to read.")],
    after: Annotated[
        str | None, Query(description="An opaque cursor from a previous page.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
) -> ApiResponse[MatchHistoryResponse]:
    """Finished matches, newest first, keyset-paginated.

    **The viewer is the authenticated user**, never a value from the
    request: `player_id` says *whose* history to read and `user.id` says who
    is asking, and a client cannot swap them. Rated matches are public;
    casual ones appear only when the viewer played in them.

    Never replays anything — an entry is stored facts, which is why a match
    whose replay is refused still appears here.
    """
    page = await history.history_for(
        player_id,
        viewer_id=user.id,
        after=decode_cursor(after) if after else None,
        limit=limit,
    )
    return build_response(MatchHistoryResponse.of(page, viewer_id=user.id))


@replay_router.get(
    "/{match_id}/replay",
    response_model=ApiResponse[MatchReplayResponse],
    status_code=status.HTTP_200_OK,
    summary="Replay a finished match",
    responses={
        **error_response(404, "No such match, or it is not visible to you"),
        **error_response(409, "This match was played under rules this build cannot reproduce"),
    },
)
async def match_replay(
    user: CurrentUser,
    replays: VisibleMatchReplayDep,
    players: ReplayPlayersDep,
    match_id: Annotated[UUID, Path(description="Which match to replay.")],
) -> ApiResponse[MatchReplayResponse]:
    """One finished match, played back ply by ply.

    A match that does not exist and a casual match the viewer did not play
    produce the **same** `404` — see this module's docstring.

    A match played under an unsupported engine version raises
    `UnsupportedEngineVersion`, which the platform's handler maps to a
    stable code. No replay is attempted: A64-014.8 refuses rather than
    approximating, because a reconstruction under fixed rules could end
    differently from the game that was rated and displayed.
    """
    replay = await replays.replay_of(match_id, viewer_id=user.id)
    if replay is None:
        raise NotFoundError("No such match.")

    # **One batched lookup of two ids** — A64-020.5E §13, and the same
    # arrangement `matchmaking`'s pending-match router makes for the same
    # reason: a match has exactly two seats, so there is no list to loop
    # over and no per-participant read to accumulate. A client compositing
    # this itself would issue one profile request per player on every
    # replay page.
    #
    # A missing entry means the account was deactivated, and the seat
    # renders with its rating and no name — the answer every other surface
    # on this platform gives for a withdrawn account.
    profiles = await players.find_public_profiles([replay.light.player_id, replay.dark.player_id])
    return build_response(MatchReplayResponse.of(replay, profiles))


__all__ = ["history_router", "replay_router"]
