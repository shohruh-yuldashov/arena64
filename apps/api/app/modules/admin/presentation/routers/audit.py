"""The admin Audit API — A64-024.8 §10.

**One route, and it only reads.** There is no `POST /admin/audit` and there
will not be: an entry is written by the privileged service performing the
action, inside that action's transaction. An endpoint that accepted entries
would let anything holding an admin session write history — including
history of things that never happened — which is the one failure an audit
trail cannot survive.

## Query shape — one page is two queries, whatever the page size

    1  the page of entries      `AuditLog`
    2  the names for it         one batch over `users.public`

Both actors and account subjects are resolved in the **same** batch: a page
of role grants names the same handful of administrators over and over, and
asking per row is the N+1 every list surface in this console has been
written to avoid.

## Why the response carries facts and not sentences

The console composes "Sanjar granted admin to Aziza" from `action`, `actor`
and `subject`, in the operator's own language. Returning the sentence would
put the platform's languages in the API and require a deployment to add
one — the same decision A64-023 made for quick messages, for the same
reason.
"""

from collections.abc import Mapping, Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.core.constants import API_V1_PREFIX
from app.modules.admin.application.ports import AuditEntryFilters
from app.modules.admin.domain.audit import AuditAction, AuditEntry, AuditSubjectType
from app.modules.admin.presentation.dependencies import CurrentAdmin
from app.modules.admin.presentation.dependencies.services import AuditLogDep
from app.modules.admin.presentation.dependencies.users import AdminUserDirectoryDep
from app.modules.admin.presentation.schemas.audit import (
    AuditActor,
    AuditEntryResponse,
    AuditPageResponse,
    AuditSubject,
)
from app.modules.users.public import AdminUserRecord

admin_audit_router = APIRouter(prefix=f"{API_V1_PREFIX}/admin/audit", tags=["admin"])

#: The largest page an operator may ask for — the same bound the other
#: admin lists use, for the same reason: there is no "all", because an
#: unbounded read of an append-only table is the query that stops working
#: exactly when the trail is long enough to be worth reading.
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 25


@admin_audit_router.get(
    "",
    response_model=AuditPageResponse,
    summary="Read the administrative audit trail",
    responses={400: {"description": "`subject_ref` was given without `subject_type`."}},
)
async def list_audit_entries(
    admin: CurrentAdmin,
    log: AuditLogDep,
    accounts: AdminUserDirectoryDep,
    response: Response,
    action: Annotated[
        AuditAction | None,
        Query(description="Narrow to one kind of action. Index-backed."),
    ] = None,
    actor_id: Annotated[
        UUID | None,
        Query(description="Everything one administrator did. Index-backed."),
    ] = None,
    subject_type: Annotated[
        AuditSubjectType | None,
        Query(description="Narrow to one kind of subject. Required alongside `subject_ref`."),
    ] = None,
    subject_ref: Annotated[
        str | None,
        Query(
            max_length=200,
            description="Everything that happened to one subject. "
            "Only accepted together with `subject_type` — the index leads with it.",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> AuditPageResponse:
    """One page of the trail, newest first.

    `admin` is unused in the body and named anyway: it is the guard, and a
    route whose protection lived only in a router-level dependency would be
    one whose protection is invisible in its own signature.
    """
    _no_store(response)

    if subject_ref is not None and subject_type is None:
        # Refused rather than silently ignored: a filter that quietly does
        # nothing shows an operator the whole trail while they believe they
        # are looking at one account's history.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`subject_ref` requires `subject_type`.",
        )

    page = await log.page(
        filters=AuditEntryFilters(
            action=action,
            actor_id=actor_id,
            subject_type=subject_type,
            subject_ref=subject_ref,
        ),
        limit=limit,
        cursor=cursor,
    )

    named = await accounts.accounts_by_ids(_account_ids(page.entries))
    return AuditPageResponse(
        items=[_entry(entry, named) for entry in page.entries],
        next_cursor=page.next_cursor,
    )


def _account_ids(entries: Sequence[AuditEntry]) -> list[UUID]:
    """Every account an entry names, actors and subjects together.

    One set, so a page of grants made by the same administrator asks for
    them once — and so the actor and the subject of a single entry cost one
    lookup between them rather than two.

    A `subject_ref` that is not a UUID is skipped rather than raised on: the
    column is `text` because a future subject need not be an account, and a
    reader that fell over on the first one would make the trail unreadable
    the day something else is audited.
    """
    identifiers: set[UUID] = set()
    for entry in entries:
        if entry.actor_id is not None:
            identifiers.add(entry.actor_id)
        if entry.subject_type is AuditSubjectType.ACCOUNT:
            try:
                identifiers.add(UUID(entry.subject_ref))
            except ValueError:
                continue
    return list(identifiers)


def _entry(entry: AuditEntry, named: Mapping[UUID, AdminUserRecord]) -> AuditEntryResponse:
    return AuditEntryResponse(
        id=entry.id,
        action=entry.action.value,
        outcome=entry.outcome.value,
        actor=AuditActor(
            type=entry.actor_type.value,
            account_id=entry.actor_id,
            username=_username(entry.actor_id, named),
        ),
        subject=AuditSubject(
            type=entry.subject_type.value,
            ref=entry.subject_ref,
            username=_subject_username(entry, named),
        ),
        before=entry.before,
        after=entry.after,
        correlation_id=entry.correlation_id,
        created_at=entry.created_at,
    )


def _username(account_id: UUID | None, named: Mapping[UUID, AdminUserRecord]) -> str | None:
    """The account's handle, or `None` for an operator and for an erased
    account.

    Not a placeholder in either case. "operator" is the actor *type* and the
    console says so; a deleted account is a fact the console shows as an id,
    because the entry's whole value is that it outlives what it describes.
    """
    if account_id is None:
        return None
    record = named.get(account_id)
    return None if record is None else record.username


def _subject_username(entry: AuditEntry, named: Mapping[UUID, AdminUserRecord]) -> str | None:
    if entry.subject_type is not AuditSubjectType.ACCOUNT:
        return None
    try:
        return _username(UUID(entry.subject_ref), named)
    except ValueError:
        return None


def _no_store(response: Response) -> None:
    """Privileged answers are never reused from a cache — §15.

    More pointed here than elsewhere: this response is the record of who
    did what, and a copy of it sitting in a shared cache is a copy nobody
    is accounting for.
    """
    response.headers["Cache-Control"] = "no-store"


__all__ = ["MAX_PAGE_SIZE", "admin_audit_router", "list_audit_entries"]
