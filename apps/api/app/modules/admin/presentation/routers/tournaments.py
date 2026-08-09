"""The admin Tournaments API — A64-024.5 §5, §14.

**Read-only.** No `POST`, `PUT`, `PATCH` or `DELETE`. `admin.audit_entry`
is unbuilt, and a tournament mutation is the most consequential unaudited
write this platform could offer: publishing a round or advancing a player
moves brackets, and brackets move ratings. Every such action waits for
A64-024.8.

## Query shape

    list    3 statements — the page, one grouped entrant count for the ids
            on it, and one batch resolving the players named by nothing
            (the list names none, so this is skipped entirely)
    detail  6 statements — the tournament, its registrations, its rounds,
            its pairings, its standings, plus one batch resolving every
            player those name

Bounded by the tournament's `capacity` rather than by anything unbounded.
§14 forbids the per-entrant and per-pairing loops this would otherwise
grow, and none exists.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.modules.admin.application.ports import TournamentLifecycleResult
from app.modules.admin.presentation.dependencies import CurrentAdmin
from app.modules.admin.presentation.dependencies.tournament_actions import (
    TournamentAdministrationDep,
)
from app.modules.admin.presentation.dependencies.tournaments import (
    AdminTournamentDirectoryDep,
)
from app.modules.admin.presentation.dependencies.users import AdminUserDirectoryDep
from app.modules.admin.presentation.schemas.tournament_actions import (
    CreateTournamentRequest,
    TournamentActionResponse,
)
from app.modules.admin.presentation.schemas.tournaments import (
    AdminEntrantView,
    AdminPairingView,
    AdminRoundView,
    AdminStandingView,
    AdminTournamentDetailResponse,
    AdminTournamentPageResponse,
    AdminTournamentSummary,
)
from app.modules.game.public.variants import ProductVariant
from app.modules.tournament.domain.tournament import TournamentFormat, TournamentStatus
from app.modules.tournament.public.administration import (
    AdminTournamentDetail,
    AdminTournamentFilters,
    AdminTournamentRecord,
)
from app.modules.users.public import AdminUserRecord

admin_tournaments_router = APIRouter(prefix="/admin/tournaments", tags=["admin"])

MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 25


@admin_tournaments_router.get(
    "", response_model=AdminTournamentPageResponse, summary="List and filter tournaments"
)
async def list_tournaments(
    admin: CurrentAdmin,
    tournaments: AdminTournamentDirectoryDep,
    response: Response,
    tournament_status: Annotated[TournamentStatus | None, Query(alias="status")] = None,
    tournament_format: Annotated[TournamentFormat | None, Query(alias="format")] = None,
    variant: Annotated[ProductVariant | None, Query()] = None,
    rated: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> AdminTournamentPageResponse:
    """One page of tournaments, newest first.

    Every filter is a typed enum or a boolean. There is no name search:
    `tournament.name` carries no index, so a substring match would be a
    sequential scan — deferred rather than added expensively (§7).
    """
    _no_store(response)

    page = await tournaments.list_tournaments(
        filters=AdminTournamentFilters(
            status=tournament_status,
            format=tournament_format,
            variant=variant,
            rated=rated,
        ),
        limit=limit,
        cursor=cursor,
    )
    return AdminTournamentPageResponse(
        items=[_summary(record) for record in page.records], next_cursor=page.next_cursor
    )


@admin_tournaments_router.get(
    "/{tournament_id}",
    response_model=AdminTournamentDetailResponse,
    summary="One tournament in full",
    responses={404: {"description": "No such tournament."}},
)
async def read_tournament(
    tournament_id: UUID,
    admin: CurrentAdmin,
    tournaments: AdminTournamentDirectoryDep,
    accounts: AdminUserDirectoryDep,
    response: Response,
) -> AdminTournamentDetailResponse:
    """One tournament, with its entrants, rounds, bracket and standings."""
    _no_store(response)

    detail = await tournaments.find_tournament(tournament_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such tournament.")

    people = await _players(accounts, detail)
    return AdminTournamentDetailResponse(
        tournament=_summary(detail.tournament),
        entrants=[
            AdminEntrantView(
                player_id=entrant.player_id,
                username=_name(entrant.player_id, people),
                display_name=_display(entrant.player_id, people),
                status=entrant.status.value,
                seed_number=entrant.seed_number,
                registered_at=entrant.registered_at,
                withdrawn_at=entrant.withdrawn_at,
            )
            for entrant in detail.entrants
        ],
        rounds=[
            AdminRoundView(
                round_number=round_.round_number,
                status=round_.status.value,
                pairing_count=round_.pairing_count,
                published_at=round_.published_at,
                started_at=round_.started_at,
                completed_at=round_.completed_at,
            )
            for round_ in detail.rounds
        ],
        pairings=[
            AdminPairingView(
                round_number=pairing.round_number,
                slot=pairing.slot,
                light_player_id=pairing.light_player_id,
                dark_player_id=pairing.dark_player_id,
                light_seed=pairing.light_seed,
                dark_seed=pairing.dark_seed,
                winner_id=pairing.winner_id,
                advancement_reason=pairing.advancement_reason,
                match_ids=list(pairing.match_ids),
            )
            for pairing in detail.pairings
        ],
        standings=[
            AdminStandingView(
                player_id=standing.player_id,
                username=_name(standing.player_id, people),
                display_name=_display(standing.player_id, people),
                final_rank=standing.final_rank,
                seed_number=standing.seed_number,
                elimination_round=standing.elimination_round,
                eliminated_by_player_id=standing.eliminated_by_player_id,
                wins=standing.wins,
                losses=standing.losses,
                draws=standing.draws,
                final_status=standing.final_status.value,
            )
            for standing in detail.standings
        ],
    )


async def _players(
    accounts: AdminUserDirectoryDep, detail: AdminTournamentDetail
) -> dict[UUID, AdminUserRecord]:
    """Every player the detail names, in **one** read — §14.

    Entrants, standings and both seats of every pairing, deduplicated
    before the call. A tournament of capacity 64 names at most 64 people
    however many nodes its bracket has, and resolving one per entrant is
    exactly the N+1 this exists to prevent.
    """
    ids: set[UUID] = {entrant.player_id for entrant in detail.entrants}
    ids |= {standing.player_id for standing in detail.standings}
    for pairing in detail.pairings:
        ids |= {pairing.light_player_id, pairing.dark_player_id} - {None}  # type: ignore[arg-type]
    return dict(await accounts.accounts_by_ids(sorted(ids)))


def _name(player_id: UUID, people: dict[UUID, AdminUserRecord]) -> str | None:
    account = people.get(player_id)
    return account.username if account else None


def _display(player_id: UUID, people: dict[UUID, AdminUserRecord]) -> str | None:
    account = people.get(player_id)
    return account.display_name if account else None


def _summary(record: AdminTournamentRecord) -> AdminTournamentSummary:
    return AdminTournamentSummary(
        tournament_id=record.tournament_id,
        name=record.name,
        format=record.format.value,
        variant=record.variant.value,
        speed_class=record.speed_class,
        status=record.status.value,
        rated=record.rated,
        capacity=record.capacity,
        entrant_count=record.entrant_count,
        registration_deadline=record.registration_deadline,
        started_at=record.started_at,
        completed_at=record.completed_at,
        created_at=record.created_at,
    )


def _no_store(response: Response) -> None:
    """Privileged answers are never reused from a cache — §19."""
    response.headers["Cache-Control"] = "no-store"


# --------------------------------------------------------------------------
# Lifecycle commands — A64-024.5H
#
# Four, and each is a transition `tournament`'s aggregate already defines.
# The route **is** the command: there is no `PATCH` and no `status` field,
# so a caller cannot ask for a move the transition table forbids or name a
# state the domain never reaches this way.
#
# Deliberately absent, and `specs/admin.md` §6.15 says why for each: round
# publication (match-driven, no manual use case), cancellation (the event
# exists and nothing produces or consumes it), entrant removal (withdrawal
# is the player's action and no administrative removal exists).
# --------------------------------------------------------------------------


@admin_tournaments_router.post(
    "",
    response_model=TournamentActionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tournament",
)
async def create_tournament(
    payload: CreateTournamentRequest,
    admin: CurrentAdmin,
    administration: TournamentAdministrationDep,
    response: Response,
) -> TournamentActionResponse:
    """Creates a tournament in `draft`, attributed to the signed-in admin.

    The id, the created-at instant and the state are the server's. So is
    `created_by`: the column is nullable and `None` means "the platform
    created it", which is a distinction a client-supplied value would
    destroy.
    """
    _no_store(response)
    created = await administration.create(
        name=payload.name,
        variant=payload.variant,
        speed_class=payload.speed_class,
        capacity=payload.capacity,
        rated=payload.rated,
        registration_deadline=payload.registration_deadline,
        actor_id=admin.id,
    )
    return _action(created)


@admin_tournaments_router.post(
    "/{tournament_id}/registration/open",
    response_model=TournamentActionResponse,
    summary="Open registration",
    responses={409: {"description": "The tournament is not in a state to open."}},
)
async def open_registration(
    tournament_id: UUID,
    admin: CurrentAdmin,
    administration: TournamentAdministrationDep,
    response: Response,
) -> TournamentActionResponse:
    """`draft` → `registration_open`.

    The precondition is checked by the aggregate under a row lock, not
    here: a check in this handler would be a second copy of the transition
    table and the copy that races.
    """
    _no_store(response)
    return _action(
        await administration.open_registration(tournament_id=tournament_id, actor_id=admin.id)
    )


@admin_tournaments_router.post(
    "/{tournament_id}/registration/close",
    response_model=TournamentActionResponse,
    summary="Close registration",
    responses={409: {"description": "The tournament is not in a state to close."}},
)
async def close_registration(
    tournament_id: UUID,
    admin: CurrentAdmin,
    administration: TournamentAdministrationDep,
    response: Response,
) -> TournamentActionResponse:
    """`registration_open` → `registration_closed`.

    Converges with `TournamentDeadlineTask`, which closes overdue
    tournaments on its own: whichever arrives first wins, and the aggregate
    refuses the second.
    """
    _no_store(response)
    return _action(
        await administration.close_registration(tournament_id=tournament_id, actor_id=admin.id)
    )


@admin_tournaments_router.post(
    "/{tournament_id}/start",
    response_model=TournamentActionResponse,
    summary="Start the tournament",
    responses={409: {"description": "The tournament is not in a state to start."}},
)
async def start_tournament(
    tournament_id: UUID,
    admin: CurrentAdmin,
    administration: TournamentAdministrationDep,
    response: Response,
) -> TournamentActionResponse:
    """`registration_closed` → `in_progress`, seeding and launching.

    **Idempotent**, by the underlying service: a second call finds the
    tournament already in progress and launches only what is missing. Two
    administrators pressing it at once resolve through the aggregate's
    `FOR UPDATE` lock.

    No body, and there is nothing one could carry — the field is frozen at
    close, the seeding is the seeding service's, and the bracket is
    arithmetic over it.
    """
    _no_store(response)
    return _action(await administration.start(tournament_id=tournament_id, actor_id=admin.id))


def _action(result: TournamentLifecycleResult) -> TournamentActionResponse:
    return TournamentActionResponse(
        tournament_id=result.tournament_id,
        status=result.status,
        matches_launched=result.matches_launched,
    )


__all__ = [
    "MAX_PAGE_SIZE",
    "admin_tournaments_router",
    "close_registration",
    "create_tournament",
    "open_registration",
    "start_tournament",
]
