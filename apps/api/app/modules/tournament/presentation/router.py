"""A tournament's public reads — SPEC-TOURNAMENT §7, A64-019.6 §9–§13.

Six read endpoints. Thin, like every other router on this platform: each
handler resolves a service, converts a parameter and maps a value to a
response — no SQL, no placement arithmetic and no visibility rule of its
own.

    GET /tournaments                       the lobby, newest first
    GET /tournaments/{id}                  the detail page
    GET /tournaments/{id}/bracket          rounds, nodes and attempts
    GET /tournaments/{id}/standings        the immutable final result
    GET /tournaments/{id}/registrations/me the viewer's own entry
    GET /players/{id}/tournaments          one player's participation

`/tournaments` and `/tournaments/{id}` differ in segment count, so no path
a caller can send matches both.

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

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.openapi import error_response
from app.api.responses import build_response
from app.core.exceptions import NotFoundError
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import (
    CurrentUser,
    OptionalCurrentUser,
    VerifiedUser,
)
from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.application.read_models import (
    RegistrationDetail,
    TournamentFilter,
)
from app.modules.tournament.domain.tournament import TournamentFormat, TournamentStatus
from app.modules.tournament.presentation.dependencies import (
    TournamentDirectoryDep,
    TournamentRegistrationServiceDep,
    TournamentResultsDep,
)
from app.modules.tournament.presentation.rate_limits import TOURNAMENT_READ_RATE_LIMIT
from app.modules.tournament.presentation.schemas.registrations import RegistrationResponse
from app.modules.tournament.presentation.schemas.results import (
    BracketResponse,
    PlayerTournamentsResponse,
    StandingsResponse,
    TournamentListResponse,
    TournamentResponse,
    decode_cursor,
    decode_list_cursor,
)
from app.modules.users.presentation.dependencies import PublicProfileReaderDep

#: Two prefixes, so each path reads as the resource it is about. A player's
#: tournaments hang off `/players` — beside their match history, which is
#: the list a client renders next to it.
tournaments_router = APIRouter(prefix="/tournaments", tags=["tournaments"])
player_tournaments_router = APIRouter(prefix="/players", tags=["tournaments"])

#: The page bounds for a player's history. Stated here because the route is
#: the only thing that bounds it — the repository takes whatever it is given.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _hidden_from(status: TournamentStatus, viewer: object | None) -> bool:
    """Whether this tournament is invisible to this viewer — §43.2.

    One place, so the lobby's predicate and the three detail guards cannot
    drift into disagreeing about what "published" means. `DRAFT` is the only
    state that hides, and only from a caller with no account.
    """
    return viewer is None and not status.is_published


@tournaments_router.get(
    "",
    response_model=ApiResponse[TournamentListResponse],
    status_code=status.HTTP_200_OK,
    summary="The tournament lobby",
    responses=error_response(422, "The pagination cursor is not valid"),
    dependencies=[Depends(TOURNAMENT_READ_RATE_LIMIT)],
)
async def tournament_lobby(
    directory: TournamentDirectoryDep,
    viewer: OptionalCurrentUser = None,
    tournament_status: Annotated[
        TournamentStatus | None, Query(alias="status", description="Only this lifecycle state.")
    ] = None,
    format: Annotated[
        TournamentFormat | None, Query(description="Only this tournament format.")
    ] = None,
    variant: Annotated[ProductVariant | None, Query(description="Only this rule set.")] = None,
    speed_class: Annotated[SpeedClass | None, Query(description="Only this speed class.")] = None,
    rated: Annotated[bool | None, Query(description="Only rated, or only casual.")] = None,
    after: Annotated[
        str | None, Query(description="An opaque cursor from a previous page.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
) -> ApiResponse[TournamentListResponse]:
    """Every public tournament, newest first — A64-020.0B.

    **Keyset**, ordered `created_at DESC, id DESC`. Total, because two
    tournaments created in the same millisecond would otherwise page
    unstably; never `OFFSET`, whose cost grows with the page number and
    whose window shifts the moment a tournament is created mid-walk.

    **Every status by default**, completed and cancelled among them. A lobby
    that hid finished tournaments would answer "what happened here?" with
    silence, and `status` is what narrows the view to what a player can
    still enter.

    **Open to a visitor with no account** since A64-026.4 §43. A tournament
    is a public competition and its bracket is a record of something that
    happened; requiring an account to look at one made the landing page
    describe a feature it could not show.

    The one narrowing is `DRAFT`, which the enum itself calls "not yet
    advertised" — a state whose operator has not decided it exists. It is
    excluded for an anonymous viewer and included for an authenticated one,
    so the lobby a player has seen since A64-020.0B is unchanged. Private
    tournaments still do not exist in v0.x; this is a lifecycle predicate,
    not a visibility flag.

    The response is identical either way: `TournamentSummary` is already the
    public read model — `created_by` is operational and was never published
    — so there is no field an anonymous caller sees less of, and none it
    sees more of.

    The five filters are a **closed set** and each is an enum or a boolean
    the tournament already stores, so an unknown value is a `422` from
    FastAPI's own validation rather than an empty page that looks like a
    combination nobody runs. There is no creator filter, no free text and no
    caller-chosen ordering: each would be either a product decision nobody
    has made or a scan whose cost this endpoint could not state.
    """
    page = await directory.listing(
        filters=TournamentFilter(
            status=tournament_status,
            format=format,
            variant=variant,
            speed_class=speed_class,
            rated=rated,
        ),
        after=decode_list_cursor(after) if after else None,
        limit=limit,
        published_only=viewer is None,
    )
    return build_response(TournamentListResponse.of(page))


@tournaments_router.get(
    "/{tournament_id}",
    response_model=ApiResponse[TournamentResponse],
    status_code=status.HTTP_200_OK,
    summary="One tournament",
    responses=error_response(404, "No such tournament"),
    dependencies=[Depends(TOURNAMENT_READ_RATE_LIMIT)],
)
async def tournament_detail(
    results: TournamentResultsDep,
    tournament_id: Annotated[UUID, Path(description="Which tournament to read.")],
    viewer: OptionalCurrentUser = None,
) -> ApiResponse[TournamentResponse]:
    """A tournament's public detail — §9, opened to anonymous in §43.

    Everything a lobby or a detail page renders: the configuration, how full
    it is, which round is being played, and the three lifecycle instants.

    A `DRAFT` tournament answers **404 to an anonymous caller**, not 403.
    The two are distinguishable and one of them is an oracle: a 403 confirms
    the id names something, which is the only fact an enumerating caller
    wants from an endpoint whose identifiers are UUIDs. Not-found is the
    same answer they would get for an id that names nothing, which is what
    makes guessing worthless.
    """
    summary = await results.summary(tournament_id)
    if summary is None or _hidden_from(summary.status, viewer):
        raise NotFoundError("That tournament does not exist.")
    return build_response(TournamentResponse.of(summary))


@tournaments_router.get(
    "/{tournament_id}/bracket",
    response_model=ApiResponse[BracketResponse],
    status_code=status.HTTP_200_OK,
    summary="A tournament's bracket",
    responses=error_response(404, "No such tournament"),
    dependencies=[Depends(TOURNAMENT_READ_RATE_LIMIT)],
)
async def tournament_bracket(
    results: TournamentResultsDep,
    players: PublicProfileReaderDep,
    tournament_id: Annotated[UUID, Path(description="Which tournament's bracket to read.")],
    viewer: OptionalCurrentUser = None,
) -> ApiResponse[BracketResponse]:
    """Every round, node and attempt — §10.

    Read whole rather than paged: a field is at most 128 (T-2), so a bracket
    is at most 127 nodes and rendering one needs all of them.

    A tournament whose bracket has not been materialised answers with an
    empty round list rather than a `404` — the tournament exists, and "no
    bracket yet" is a state a client renders rather than an error.

    **One batched identity lookup for the whole bracket** — A64-020.6 §26,
    and the same arrangement `game`'s history and replay routes already
    make. Without it a client turns each seat into a name by asking, and a
    128-player field is 128 requests behind one page.
    """
    # A64-026.4 §43.2. The same guard the detail applies, for the same
    # reason: a draft answers 404 to an anonymous caller rather than 403,
    # because 403 confirms the id names something.
    summary = await results.summary(tournament_id)
    if summary is None or _hidden_from(summary.status, viewer):
        raise NotFoundError("That tournament does not exist.")

    bracket = await results.bracket(tournament_id)
    entrants = BracketResponse.participant_ids_in(bracket)
    profiles = await players.find_public_profiles(entrants) if entrants else {}
    return build_response(BracketResponse.of(bracket, profiles))


@tournaments_router.get(
    "/{tournament_id}/standings",
    response_model=ApiResponse[StandingsResponse],
    status_code=status.HTTP_200_OK,
    summary="A tournament's final standings",
    dependencies=[Depends(TOURNAMENT_READ_RATE_LIMIT)],
    responses=error_response(404, "No such tournament"),
)
async def tournament_standings(
    results: TournamentResultsDep,
    players: PublicProfileReaderDep,
    tournament_id: Annotated[UUID, Path(description="Which tournament's results to read.")],
    viewer: OptionalCurrentUser = None,
) -> ApiResponse[StandingsResponse]:
    """The immutable final placement — §11.

    Ordered by rank, then seed, then player id. **Empty while the tournament
    is being played**: standings are materialised once, when it completes
    (§6f), and nothing here derives a partial one — a placement that changed
    between two reads would not be a result.

    Identities are composed in one batched read, for the bracket's reason.
    An empty placing costs no lookup at all.
    """
    # A64-026.4 §43.2. The same guard the detail applies, for the same
    # reason: a draft answers 404 to an anonymous caller rather than 403,
    # because 403 confirms the id names something.
    summary = await results.summary(tournament_id)
    if summary is None or _hidden_from(summary.status, viewer):
        raise NotFoundError("That tournament does not exist.")

    standings = await results.standings(tournament_id)
    entrants = StandingsResponse.participant_ids_in(standings)
    profiles = await players.find_public_profiles(entrants) if entrants else {}
    return build_response(StandingsResponse.of(tournament_id, standings, profiles))


@tournaments_router.get(
    "/{tournament_id}/registrations/me",
    response_model=ApiResponse[RegistrationResponse],
    status_code=status.HTTP_200_OK,
    summary="Your own entry in a tournament",
    responses=error_response(404, "No such tournament, or you never entered it"),
)
async def my_registration(
    user: CurrentUser,
    results: TournamentResultsDep,
    tournament_id: Annotated[UUID, Path(description="Which tournament to read your entry in.")],
) -> ApiResponse[RegistrationResponse]:
    """**The authenticated player's own** entry — A64-020.6 §8.

    The read half of the two participant writes, and the reason it exists:
    a detail page has to know whether the viewer is in this tournament
    before it can offer to enter or leave it, and until now that fact was
    only observable by *attempting* the write. Deriving it from whether a
    button appeared inverts the authority — the record is the server's.

    `/me` rather than a player id, so the endpoint has no shape in which it
    reads somebody else's entry. That makes this narrower than the public
    history at `/players/{id}/tournaments`, which is deliberate: whether a
    player entered a tournament is public there, one page at a time, and
    finding a *particular* tournament in it is an unbounded walk.

    **`404` for "never entered", and it is not an error.** A client asking
    "am I in this?" is asking a question whose negative answer is normal, so
    the code is what it reads rather than an exception to log — the same
    answer `DELETE …/registrations/me` gives for the same absence.

    A **withdrawn** entry answers `200` with `status = "withdrawn"`, not
    `404`: the row survives withdrawal (§7's append-oriented record), and
    "you left this one" is a different fact from "you were never here" —
    one of them means re-entering is possible while registration is open.
    """
    detail = await results.registration_of(tournament_id, user.id)
    if detail is None:
        raise NotFoundError("You have no entry in that tournament.")
    return build_response(RegistrationResponse.of(detail))


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
    user: VerifiedUser,
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
    user: VerifiedUser,
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
    responses=error_response(422, "The pagination cursor is not valid"),
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
