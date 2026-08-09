"""The admin Users API — A64-024.3 §2, §10.

**Read-only.** There is no mutation here, and that is a decision rather than
an omission: §9 requires a security-sensitive admin mutation to produce an
audit entry, and `admin.audit_entry` is designed in `database.md` §10.4 and
**not built** — A64-024.1 created `role_assignment` alone. An unaudited
deactivation is precisely the thing §8 says to stop before, so this phase
reads and A64-024.8 unlocks the writes. `specs/admin.md` §10 records it.

## Query shape — one page is three queries, whatever the page size

    1  the page of accounts        `users.public.AdministrativeUserDirectory`
    2  the admin roles for it      one batch over `admin.role_assignment`

Nothing here loops a read. §10 forbids querying each user's role
individually, and a batch per *dimension* rather than per row is what keeps
a fifty-row page the same cost as a five-row one.

The detail route is the same shape at `n = 1`.

## Ratings are deliberately absent

§6 admits them "if cheap", and they are not. `RatingReader` is batched by
`(player, key)` pairs, so a detail page would first have to enumerate every
`variant × speed_class` the product offers to ask about them — which is
product knowledge this router has no business holding, and a second module's
catalogue to keep in step. An operator investigating an account does not
need a rating to do it, so this waits for a phase that has a reason.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.core.constants import API_V1_PREFIX
from app.modules.admin.domain.roles import AdminRole
from app.modules.admin.presentation.dependencies import AdminRoleServiceDep, CurrentAdmin
from app.modules.admin.presentation.dependencies.moderation import (
    ModerationCaseReaderDep,
    ModerationServiceDep,
)
from app.modules.admin.presentation.dependencies.users import AdminUserDirectoryDep
from app.modules.admin.presentation.routers.moderation import moderation_state_for
from app.modules.admin.presentation.schemas.users import (
    AdminUserDetail,
    AdminUserPageResponse,
    AdminUserSummary,
)
from app.modules.users.public import AdminUserFilters, AdminUserRecord

admin_users_router = APIRouter(prefix=f"{API_V1_PREFIX}/admin/users", tags=["admin"])

#: The largest page an operator may ask for — §3.
#:
#: Fifty rows is a screenful on a desktop and a bounded query whatever the
#: term. There is deliberately no "all": an unbounded list is the one shape
#: that stops working exactly when the platform is worth administering.
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 25


@admin_users_router.get(
    "",
    response_model=AdminUserPageResponse,
    summary="Search and list accounts",
)
async def list_users(
    admin: CurrentAdmin,
    directory: AdminUserDirectoryDep,
    roles: AdminRoleServiceDep,
    response: Response,
    q: str | None = Query(
        default=None,
        max_length=120,
        description="Matches a username or an email address by **prefix**. "
        "Substring matching is not offered — it cannot use either index.",
    ),
    is_active: bool | None = Query(default=None),
    is_verified: bool | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
) -> AdminUserPageResponse:
    """One page of accounts, newest first.

    `admin` is unused in the body and named anyway: it is the guard, and a
    route that took it only as a router-level dependency would be one whose
    protection is invisible in its own signature.
    """
    _no_store(response)

    page = await directory.list_accounts(
        term=q or None,
        filters=AdminUserFilters(is_active=is_active, is_verified=is_verified),
        limit=limit,
        cursor=cursor,
    )

    administrators = await _administrators(roles)
    return AdminUserPageResponse(
        items=[_summary(record, administrators) for record in page.records],
        next_cursor=page.next_cursor,
    )


@admin_users_router.get(
    "/{user_id}",
    response_model=AdminUserDetail,
    summary="One account in full",
    responses={404: {"description": "No such account."}},
)
async def read_user(
    user_id: UUID,
    admin: CurrentAdmin,
    directory: AdminUserDirectoryDep,
    roles: AdminRoleServiceDep,
    moderation: ModerationServiceDep,
    cases: ModerationCaseReaderDep,
    response: Response,
) -> AdminUserDetail:
    """One account, composed from published ports only."""
    _no_store(response)

    record = await directory.find_account(user_id)
    if record is None:
        # A plain `404`. The caller is already an authenticated
        # administrator, so there is no enumeration concern to collapse
        # this into something vaguer.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such account.")

    grant = await roles.live_grant(account_id=user_id, role=AdminRole.ADMIN)
    # A64-024.6. One more read on the detail page, not on the list: an
    # operator opening one account wants to know whether they can sign in,
    # and a page of fifty does not.
    moderation_state = await moderation_state_for(
        user_id, moderation=moderation, cases=cases, accounts=directory
    )

    return AdminUserDetail(
        id=record.id,
        username=record.username,
        display_name=record.display_name,
        email=record.email,
        is_active=record.is_active,
        is_verified=record.is_verified,
        created_at=record.created_at,
        is_admin=grant is not None,
        admin_role_granted_at=grant.granted_at if grant is not None else None,
        moderation=moderation_state,
    )


async def _administrators(roles: AdminRoleServiceDep) -> frozenset[UUID]:
    """Every account currently holding `ADMIN`, in one read.

    A whole-set read rather than a per-row lookup, and it is cheap for a
    reason worth stating: administrators are a handful of accounts by
    definition, so the set is smaller than any page it annotates. That is
    what makes "is this row an admin" a membership test instead of the
    fifty queries §10 forbids.
    """
    return frozenset(await roles.holders_of(AdminRole.ADMIN))


def _summary(record: AdminUserRecord, administrators: frozenset[UUID]) -> AdminUserSummary:
    return AdminUserSummary(
        id=record.id,
        username=record.username,
        display_name=record.display_name,
        email=record.email,
        is_active=record.is_active,
        is_verified=record.is_verified,
        created_at=record.created_at,
        is_admin=record.id in administrators,
    )


def _no_store(response: Response) -> None:
    """Privileged answers are never reused from a cache — §15."""
    response.headers["Cache-Control"] = "no-store"


__all__ = ["MAX_PAGE_SIZE", "admin_users_router"]
