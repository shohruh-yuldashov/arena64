"""The admin Moderation API — A64-024.6.

**The first admin router that writes.** Every phase before it was read-only
because `admin.audit_entry` was unbuilt (`specs/admin.md` §7); A64-024.8
built it, and these two mutations are what it unblocked.

    GET  /api/v1/admin/moderation                  who is restricted
    POST /api/v1/admin/users/{user_id}/restrict    withhold access
    POST /api/v1/admin/users/{user_id}/restore     give it back

The two mutations live on the user's path rather than under `/moderation`
because the target is an account and the path segment is where a target
belongs — a body field naming the subject would be one a caller could
change independently of the URL they were authorised against.

## Every write goes through one service

Nothing here calls a repository. `ModerationService` owns the safety rules
— self-restriction, the last administrator, duplicates — and a route
holding a repository could write a row past all three.

## The actor is never in the request

`CurrentAdmin` resolves it, and the schemas have no field for it. That is
§12's invariant made structural rather than checked.
"""

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.core.constants import API_V1_PREFIX
from app.modules.admin.domain.moderation import ModerationCase, Sanction
from app.modules.admin.domain.roles import AdminRole
from app.modules.admin.presentation.dependencies import AdminRoleServiceDep, CurrentAdmin
from app.modules.admin.presentation.dependencies.moderation import (
    ModerationCaseReaderDep,
    ModerationServiceDep,
)
from app.modules.admin.presentation.dependencies.users import AdminUserDirectoryDep
from app.modules.admin.presentation.schemas.moderation import (
    AccountModerationState,
    ModerationCaseView,
    RestrictAccountRequest,
    SanctionPageResponse,
    SanctionView,
)
from app.modules.users.public import AdminUserRecord

admin_moderation_router = APIRouter(prefix=f"{API_V1_PREFIX}/admin", tags=["admin"])

#: The largest page an operator may ask for — the bound every admin list
#: on this console uses. There is no "all".
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 25


@admin_moderation_router.get(
    "/moderation",
    response_model=SanctionPageResponse,
    summary="List account restrictions",
)
async def list_restrictions(
    admin: CurrentAdmin,
    moderation: ModerationServiceDep,
    cases: ModerationCaseReaderDep,
    accounts: AdminUserDirectoryDep,
    response: Response,
    effective_only: Annotated[
        bool,
        Query(
            description="Only restrictions in force right now. `false` includes "
            "expired and lifted ones, which are history rather than deletions."
        ),
    ] = True,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> SanctionPageResponse:
    """One page of restrictions, newest first.

    Three statements whatever the page size: the page, one batch of cases,
    one batch of accounts. Nothing here loops a read — a page of fifty
    restrictions names at most a hundred accounts and asks for them once.

    `admin` is unused in the body and named anyway: it is the guard, and a
    route whose protection lived only in a router-level dependency would be
    one whose protection is invisible in its own signature.
    """
    _no_store(response)

    page = await moderation.page(effective_only=effective_only, limit=limit, cursor=cursor)
    views = await _views_for(page.sanctions, cases=cases, accounts=accounts, moderation=moderation)
    return SanctionPageResponse(items=views, next_cursor=page.next_cursor)


@admin_moderation_router.post(
    "/users/{user_id}/restrict",
    response_model=SanctionView,
    status_code=status.HTTP_201_CREATED,
    summary="Withhold access from an account",
    responses={
        404: {"description": "No such account."},
        409: {"description": "Already restricted, or the last administrator."},
        422: {"description": "An administrator cannot restrict themselves."},
    },
)
async def restrict_account(
    user_id: UUID,
    payload: RestrictAccountRequest,
    admin: CurrentAdmin,
    moderation: ModerationServiceDep,
    roles: AdminRoleServiceDep,
    cases: ModerationCaseReaderDep,
    accounts: AdminUserDirectoryDep,
    response: Response,
) -> SanctionView:
    """Restricts `user_id`, recording the decision that authorised it.

    The account is read **first**, so restricting an id that matches
    nothing is a `404` rather than a moderation case about nobody. The
    holder set is read next and handed to the service, which is where the
    last-administrator refusal lives.

    Every refusal the service raises maps to a status through the
    platform's exception taxonomy, and each writes a `FAILED` audit entry
    before raising — see `ModerationService`.
    """
    _no_store(response)

    if await accounts.find_account(user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such account.")

    expires_at = None
    if payload.duration_hours is not None:
        # Computed against the **service's** clock, not the browser's — see
        # `RestrictAccountRequest` on why a duration travels instead of an
        # instant.
        expires_at = moderation.now() + timedelta(hours=payload.duration_hours)

    sanction = await moderation.suspend(
        player_id=user_id,
        category=payload.category,
        reasoning=payload.reasoning,
        expires_at=expires_at,
        actor_id=admin.id,
        administrators=await roles.holders_of(AdminRole.ADMIN),
    )

    views = await _views_for([sanction], cases=cases, accounts=accounts, moderation=moderation)
    return views[0]


@admin_moderation_router.post(
    "/users/{user_id}/restore",
    response_model=SanctionView,
    summary="Restore access to a restricted account",
    responses={
        404: {"description": "No such account."},
        409: {"description": "That account is not restricted."},
    },
)
async def restore_account(
    user_id: UUID,
    admin: CurrentAdmin,
    moderation: ModerationServiceDep,
    cases: ModerationCaseReaderDep,
    accounts: AdminUserDirectoryDep,
    response: Response,
) -> SanctionView:
    """Lifts the live restriction on `user_id`, naming who lifted it.

    **No body.** There is nothing to decide: a restore ends the one live
    restriction, and a reason for ending it would be a second taxonomy
    nobody reads. The lift is attributed and audited, which is what §13.3
    asks for.

    Sessions are not reinstated — see `ModerationService.restore`.
    """
    _no_store(response)

    if await accounts.find_account(user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such account.")

    lifted = await moderation.restore(player_id=user_id, actor_id=admin.id)
    views = await _views_for([lifted], cases=cases, accounts=accounts, moderation=moderation)
    return views[0]


async def moderation_state_for(
    user_id: UUID,
    *,
    moderation: ModerationServiceDep,
    cases: ModerationCaseReaderDep,
    accounts: AdminUserDirectoryDep,
) -> AccountModerationState:
    """One account's standing, for the Users detail page — A64-024.3 §6.

    Published as a function rather than as a second route so the detail
    page stays one request. Returns the **effective** restriction only: the
    badge answers "can this person sign in right now", and history lives at
    `/moderation`.
    """
    effective = await moderation.effective_for(user_id)
    if not effective:
        return AccountModerationState(is_restricted=False)

    views = await _views_for(effective, cases=cases, accounts=accounts, moderation=moderation)
    return AccountModerationState(is_restricted=True, restriction=views[0])


async def _views_for(
    sanctions: Sequence[Sanction],
    *,
    cases: ModerationCaseReaderDep,
    accounts: AdminUserDirectoryDep,
    moderation: ModerationServiceDep,
) -> list[SanctionView]:
    """Composes the response, resolving cases and names in **two** batches.

    Two reads whatever the page size. The naive shape here is one case
    lookup and one account lookup per row, which on a fifty-row page is a
    hundred queries — the N+1 every list surface on this console has been
    written to avoid.
    """
    rows = list(sanctions)
    if not rows:
        return []

    by_case = await cases.cases_by_ids([row.case_id for row in rows])
    named = await accounts.accounts_by_ids(
        [row.player_id for row in rows] + [case.opened_by for case in by_case.values()]
    )
    now = moderation.now()

    views: list[SanctionView] = []
    for row in rows:
        case = by_case.get(row.case_id)
        if case is None:  # pragma: no cover — the FK makes this unreachable
            continue
        views.append(
            SanctionView(
                id=row.id,
                player_id=row.player_id,
                username=_username(row.player_id, named),
                kind=row.kind,
                is_effective=row.is_effective_at(now),
                starts_at=row.starts_at,
                expires_at=row.expires_at,
                lifted_at=row.lifted_at,
                lifted_by=row.lifted_by,
                case=_case_view(case, named),
            )
        )
    return views


def _case_view(case: ModerationCase, named: Mapping[UUID, AdminUserRecord]) -> ModerationCaseView:
    return ModerationCaseView(
        id=case.id,
        category=case.category.value,
        decision=case.decision,
        reasoning=case.reasoning,
        opened_by=case.opened_by,
        opened_by_username=_username(case.opened_by, named),
        opened_at=case.opened_at,
    )


def _username(account_id: UUID, named: Mapping[UUID, AdminUserRecord]) -> str | None:
    """The account's handle, or `None` for one that no longer exists.

    Not a placeholder: a moderation record outlives the account it names,
    and an id is a fact where a fabricated name would not be.
    """
    record = named.get(account_id)
    return None if record is None else record.username


def _no_store(response: Response) -> None:
    """Privileged answers are never reused from a cache — §15."""
    response.headers["Cache-Control"] = "no-store"


__all__ = [
    "MAX_PAGE_SIZE",
    "admin_moderation_router",
    "list_restrictions",
    "moderation_state_for",
    "restore_account",
    "restrict_account",
]
