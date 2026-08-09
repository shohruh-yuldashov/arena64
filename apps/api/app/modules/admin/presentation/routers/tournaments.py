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

from app.modules.admin.presentation.dependencies import CurrentAdmin
from app.modules.admin.presentation.dependencies.tournaments import (
    AdminTournamentDirectoryDep,
)
from app.modules.admin.presentation.dependencies.users import AdminUserDirectoryDep
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


__all__ = ["MAX_PAGE_SIZE", "admin_tournaments_router"]
