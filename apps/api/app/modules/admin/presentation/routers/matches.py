"""The admin Matches API — A64-024.4 §4, §8.

**Read-only.** No `POST`, no `PUT`, no `PATCH`, no `DELETE`. `admin.audit_entry`
is specified in `database.md` §10.4 and is not built, so an administrative
match mutation would be unattributable — and a mutation that ends somebody's
rated game without a record is the one this platform must not ship first.
Force-finish, cancel, result editing and rollback all wait for A64-024.8.

## Query shape — two queries per page, whatever the page size

    1  the page of matches        `game.public.AdministrativeMatchDirectory`
    2  the participants' names    `users.public.AdministrativeUserDirectory`
                                  `.accounts_by_ids`, one batch for both
                                  seats of every row

A fifty-row page names up to a hundred players and resolves them in **one**
statement. §8 forbids the per-row alternative, which is the shape this
endpoint would naturally grow into: `for match in page: fetch(match.light)`.

The detail route is the same at `n = 1`.

## Names are resolved, not joined

`match` lives in the `game` schema and accounts live in `users`. DB-03
forbids a cross-schema join, so the two reads are two queries composed in
this router — which is also what keeps either module free to move.
"""

from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.modules.admin.presentation.dependencies import CurrentAdmin
from app.modules.admin.presentation.dependencies.matches import AdminMatchDirectoryDep
from app.modules.admin.presentation.dependencies.users import AdminUserDirectoryDep
from app.modules.admin.presentation.schemas.matches import (
    AdminMatchDetail,
    AdminMatchPageResponse,
    AdminMatchParticipant,
    AdminMatchSummary,
    AdminMatchTimeControl,
)
from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.domain.variants import MatchOrigin, ProductVariant
from app.modules.game.public import AdminMatchFilters, AdminMatchRecord
from app.modules.users.public import AdminUserRecord

admin_matches_router = APIRouter(prefix="/admin/matches", tags=["admin"])

MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 25


@admin_matches_router.get(
    "", response_model=AdminMatchPageResponse, summary="List and filter matches"
)
async def list_matches(
    admin: CurrentAdmin,
    matches: AdminMatchDirectoryDep,
    accounts: AdminUserDirectoryDep,
    response: Response,
    match_status: Annotated[MatchRecordStatus | None, Query(alias="status")] = None,
    rated: Annotated[bool | None, Query()] = None,
    variant: Annotated[ProductVariant | None, Query()] = None,
    origin: Annotated[MatchOrigin | None, Query()] = None,
    participant_id: Annotated[
        UUID | None,
        Query(
            description="Matches either seat. Search by *name* is two steps: find "
            "the account on the Users console, then filter here by its id."
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> AdminMatchPageResponse:
    """One page of matches, newest first.

    Every filter is a typed enum or a boolean, so there is no query
    language and no free-text predicate reaching the database.
    """
    _no_store(response)

    page = await matches.list_matches(
        filters=AdminMatchFilters(
            status=match_status,
            rated=rated,
            variant=variant,
            origin=origin,
            participant_id=participant_id,
        ),
        limit=limit,
        cursor=cursor,
    )

    people = await _participants(accounts, page.records)
    return AdminMatchPageResponse(
        items=[_summary(record, people) for record in page.records],
        next_cursor=page.next_cursor,
    )


@admin_matches_router.get(
    "/{match_id}",
    response_model=AdminMatchDetail,
    summary="One match in full",
    responses={404: {"description": "No such match."}},
)
async def read_match(
    match_id: UUID,
    admin: CurrentAdmin,
    matches: AdminMatchDirectoryDep,
    accounts: AdminUserDirectoryDep,
    response: Response,
) -> AdminMatchDetail:
    """One match, composed from two published ports."""
    _no_store(response)

    record = await matches.find_match(match_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such match.")

    people = await _participants(accounts, [record])
    summary = _summary(record, people)

    return AdminMatchDetail(
        **summary.model_dump(),
        settled_at=record.settled_at,
        time_control=(
            AdminMatchTimeControl(
                initial_ms=record.time_control.initial_ms,
                increment_ms=record.time_control.increment_ms,
            )
            if record.time_control is not None
            else None
        ),
    )


async def _participants(
    accounts: AdminUserDirectoryDep, records: Sequence[AdminMatchRecord]
) -> dict[UUID, AdminUserRecord]:
    """Every player named by the page, in **one** read — §8.

    Deduplicated before the call, so a page where one player appears in
    several matches asks for them once. This is the N+1 the endpoint would
    otherwise grow: resolving a name per row is the obvious implementation
    and the wrong one.
    """
    ids = {record.light_player_id for record in records} | {
        record.dark_player_id for record in records
    }
    return dict(await accounts.accounts_by_ids(sorted(ids)))


def _seat(player_id: UUID, side: str, people: dict[UUID, AdminUserRecord]) -> AdminMatchParticipant:
    """One seat, with a name when the account still resolves.

    **No email and no account state** — §11. A match page shows who played;
    anything more about the person is `/users/{id}`, which is a different
    page with its own decision about what to expose.
    """
    account = people.get(player_id)
    return AdminMatchParticipant(
        player_id=player_id,
        username=account.username if account else None,
        display_name=account.display_name if account else None,
        side=side,
    )


def _summary(record: AdminMatchRecord, people: dict[UUID, AdminUserRecord]) -> AdminMatchSummary:
    return AdminMatchSummary(
        match_id=record.match_id,
        status=record.status.value,
        variant=record.variant.value,
        rated=record.rated,
        origin=record.origin.value,
        light=_seat(record.light_player_id, "light", people),
        dark=_seat(record.dark_player_id, "dark", people),
        outcome=record.outcome.value if record.outcome else None,
        winner=record.winner.value if record.winner else None,
        termination_reason=(record.termination_reason.value if record.termination_reason else None),
        speed_class=record.speed_class,
        ply_number=record.ply_number,
        created_at=record.created_at,
        ended_at=record.ended_at,
    )


def _no_store(response: Response) -> None:
    """Privileged answers are never reused from a cache — §4."""
    response.headers["Cache-Control"] = "no-store"


__all__ = ["MAX_PAGE_SIZE", "admin_matches_router"]
