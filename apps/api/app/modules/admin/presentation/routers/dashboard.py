"""The admin dashboard API — A64-024.9.

    GET /api/v1/admin/dashboard

**One endpoint, deliberately.** The alternative — the console calling six
admin listings and counting the rows — would be six round trips to render one
page, and each would fetch columns nothing on it displays. Worse, "how many
matches are active" answered by paging a match listing is a number bounded by
the page size rather than by reality.

**Read-only, and it is the last admin surface that could have grown a
mutation.** Every card links to the console that owns the action instead; a
retry button beside a failure count is one clicked without reading which
failure it was.
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from fastapi import APIRouter, Response

from app.modules.admin.application.services.dashboard_service import DashboardOverview
from app.modules.admin.domain.audit import AuditEntry
from app.modules.admin.presentation.dependencies import CurrentAdmin
from app.modules.admin.presentation.dependencies.dashboard import DashboardServiceDep
from app.modules.admin.presentation.dependencies.users import AdminUserDirectoryDep
from app.modules.admin.presentation.schemas.dashboard import (
    AccountsCard,
    ActivityEntry,
    AttentionCard,
    DashboardResponse,
    MatchesCard,
    TournamentsCard,
)
from app.modules.users.public import AdminUserRecord

admin_dashboard_router = APIRouter(prefix="/admin/dashboard", tags=["admin"])


@admin_dashboard_router.get(
    "",
    response_model=DashboardResponse,
    summary="The operator overview",
)
async def read_dashboard(
    admin: CurrentAdmin,
    dashboard: DashboardServiceDep,
    accounts: AdminUserDirectoryDep,
    response: Response,
) -> DashboardResponse:
    """Six facts and the ten most recent privileged actions.

    Seven reads plus one batch, and the count does not move with the size of
    any table — see `DashboardService`.

    `admin` is unused in the body and named anyway: it is the guard, and a
    route whose protection lived only in a router-level dependency would be
    one whose protection is invisible in its own signature.
    """
    _no_store(response)

    overview = await dashboard.overview()
    named = await accounts.accounts_by_ids(_actor_ids(overview.recent_activity))
    return _response(overview, named)


def _actor_ids(entries: Sequence[AuditEntry]) -> list[UUID]:
    """Every administrator the activity list names, deduplicated.

    One batch rather than a lookup per row — the same shape `/audit` uses,
    and here it matters more: ten entries by one administrator would
    otherwise be ten reads of the same account.

    Operator entries name nobody and contribute nothing to ask for.
    """
    return list({entry.actor_id for entry in entries if entry.actor_id is not None})


def _response(
    overview: DashboardOverview, named: Mapping[UUID, AdminUserRecord]
) -> DashboardResponse:
    return DashboardResponse(
        accounts=AccountsCard(
            registered_last_day=overview.accounts.registered_last_day,
            registered_last_week=overview.accounts.registered_last_week,
        ),
        matches=MatchesCard(
            active=overview.matches.active,
            awaiting_acceptance=overview.matches.awaiting_acceptance,
        ),
        tournaments=TournamentsCard(
            registration_open=overview.tournaments.registration_open,
            in_progress=overview.tournaments.in_progress,
        ),
        attention=AttentionCard(
            restrictions_in_force=overview.restrictions_in_force,
            push_deliveries_retry_exhausted=overview.deliveries.retry_exhausted,
        ),
        recent_activity=[_activity(entry, named) for entry in overview.recent_activity],
        generated_at=overview.generated_at,
    )


def _activity(entry: AuditEntry, named: Mapping[UUID, AdminUserRecord]) -> ActivityEntry:
    record = named.get(entry.actor_id) if entry.actor_id is not None else None
    return ActivityEntry(
        id=entry.id,
        action=entry.action.value,
        outcome=entry.outcome.value,
        actor_type=entry.actor_type.value,
        actor_id=entry.actor_id,
        # `None` for an operator action, which named nobody, and for an
        # administrator whose account has since been erased — the trail
        # outlives what it describes, and an id is a fact where a fabricated
        # name would not be.
        actor_username=None if record is None else record.username,
        subject_type=entry.subject_type.value,
        subject_ref=entry.subject_ref,
        created_at=entry.created_at,
    )


def _no_store(response: Response) -> None:
    """Privileged answers are never reused from a cache — §15."""
    response.headers["Cache-Control"] = "no-store"


__all__ = ["admin_dashboard_router", "read_dashboard"]
