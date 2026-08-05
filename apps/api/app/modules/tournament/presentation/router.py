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

## What writes, and what deliberately does not — A64-019.8

Two participant writes: entering a tournament and leaving it. Both act on
**the authenticated player only**, and neither has a shape that could name
somebody else — no `player_id` in a body, no `player_id` in a path, `/me`
where an identifier would otherwise go.

Everything an operator does — creating a tournament, opening and closing
registration, seeding, starting — is **not here**, and its absence is a
decision rather than an omission. Those commands need an administrator, and
this platform has no administrator: no role, no scope claim, no permission
and no operator credential exist anywhere in `auth` or `users`. Putting
them behind `CurrentUser` would make every registered player able to create
tournaments and close other people's registrations.

So they live in `app/operator/tournament.py`, a separate process entry
point in the shape `main.py` already names for the gateway, worker and
clock profiles — reachable by whoever can run a process on the host, and by
nobody over HTTP. See `specs/tournament/audit.md` §4.

There is also no `POST /complete`: completion is automatic (§14), and an
endpoint that could finish a bracket is one that could finish an
*unfinished* one.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, status

from app.api.openapi import error_response
from app.api.responses import build_response
from app.core.exceptions import NotFoundError
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.tournament.application.read_models import RegistrationDetail
from app.modules.tournament.presentation.dependencies import (
    TournamentRegistrationServiceDep,
    TournamentResultsDep,
)
from app.modules.tournament.presentation.schemas.registrations import RegistrationResponse
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


@tournaments_router.post(
    "/{tournament_id}/registrations",
    response_model=ApiResponse[RegistrationResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Enter a tournament",
    responses={
        **error_response(404, "No such tournament, or no such player"),
        **error_response(
            409,
            "Registration is closed, the deadline passed, the field is full, "
            "or you already entered",
        ),
    },
)
async def enter_tournament(
    user: CurrentUser,
    registrations: TournamentRegistrationServiceDep,
    results: TournamentResultsDep,
    tournament_id: Annotated[UUID, Path(description="Which tournament to enter.")],
) -> ApiResponse[RegistrationResponse]:
    """Enters **the authenticated player** — A64-019.8 §1.

    There is no request body. The player is `user.id` and nothing else:
    the route has no parameter that could name somebody else, so entering
    another person is not refused, it is unrepresentable.

    Every refusal is the service's, raised under the row lock and mapped to
    a stable code by its own type — `registration_not_open`,
    `registration_deadline_passed`, `tournament_full`, `already_registered`.
    Nothing is re-checked here: a route that repeated the capacity test
    would be a second copy that could disagree, and it would be outside the
    lock that makes the first one correct.

    **Idempotency is a conflict, not a repeat.** A second entry answers
    `409 already_registered` rather than `201`, because the unique key
    refused an insert and pretending otherwise would report a registration
    this call did not make. A client reconciling a dropped response reads
    the code and treats it as success.
    """
    await registrations.register(tournament_id, user.id)
    return build_response(RegistrationResponse.of(await _entry_of(results, tournament_id, user.id)))


@tournaments_router.delete(
    "/{tournament_id}/registrations/me",
    response_model=ApiResponse[RegistrationResponse],
    status_code=status.HTTP_200_OK,
    summary="Withdraw from a tournament",
    responses={
        **error_response(404, "No such tournament, or you have no live entry"),
        **error_response(409, "Registration has closed and the field is fixed"),
    },
)
async def withdraw_from_tournament(
    user: CurrentUser,
    registrations: TournamentRegistrationServiceDep,
    results: TournamentResultsDep,
    tournament_id: Annotated[UUID, Path(description="Which tournament to leave.")],
) -> ApiResponse[RegistrationResponse]:
    """Withdraws **the authenticated player's own** entry — §1.

    `/me` rather than a player id in the path, so there is no shape in
    which this endpoint acts on somebody else — the same reason the entry
    route has no body.

    Allowed **only before registration closes**. After that the field is
    fixed and the bracket is built from exactly those players, so a
    withdrawal would leave a seat nothing fills; it is refused rather than
    converted to a forfeit, because a forfeit is a *match* outcome and
    there is no match yet.

    The row survives with `status = withdrawn` — §7's append-oriented
    record. A repeated withdrawal answers `404 registration_not_found`,
    which is the same answer as never having entered and is what makes the
    call safe to send twice: the resource `/registrations/me` is gone
    either way, and `withdrawn_at` cannot move.
    """
    await registrations.withdraw(tournament_id, user.id)
    return build_response(RegistrationResponse.of(await _entry_of(results, tournament_id, user.id)))


async def _entry_of(
    results: TournamentResultsDep, tournament_id: UUID, player_id: UUID
) -> RegistrationDetail:
    """The written entry, read back in one statement.

    Read rather than assembled from what the service happened to know: the
    seed and the tournament's status both live outside the `Registration`
    value, and inferring them would produce a response that is right only
    until somebody reuses the endpoint.

    Absence here is unreachable — the write above committed the row — and
    is answered as `404` rather than asserted, because a `500` on a
    successful write is the worse of the two failures.
    """
    detail = await results.registration_of(tournament_id, player_id)
    if detail is None:  # pragma: no cover — the write above committed it
        raise NotFoundError("That registration does not exist.")
    return detail


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
