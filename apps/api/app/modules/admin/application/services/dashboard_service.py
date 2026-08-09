"""`DashboardService` — the operator's first screen. A64-024.9.

Composes six facts from the modules that own them, plus the audit trail's
most recent entries. It computes nothing: every number below is produced by
the module whose data it describes, through that module's published
administrative port.

## What this is not

Not a metrics system, not an analytics surface, not a report. Arena64 emits
its metrics as structured log records (`specs/live-game/audit.md` §8 — there
is no Prometheus, StatsD or OpenTelemetry collector in this deployment), and
they are consumed by whatever log pipeline a deployment runs. Recomputing
any of them here as a SQL aggregate would create a **second answer** to the
same question, and the two would disagree exactly when somebody was relying
on them.

What this screen is for is different and narrower: is anything happening
right now, and is anything waiting for a person.

## The query budget, and why it is fixed

    accounts        1   two windows, one range scan
    matches         1   grouped, over a partial index
    tournaments     1   grouped
    restrictions    1   over a partial index
    push deliveries 1   over a partial index
    recent audit    1 + 1 batch resolving every actor named

**Seven reads, and the number does not move.** Not with the account count,
not with the match history, not with the audit trail's length. The one thing
that could have grown it — an actor lookup per audit row — is the batch, for
the reason every list on this console has one.

## It is atomic, and that is a decision

All seven reads share the request's session. If one fails the request fails,
and the console shows an error rather than a page of numbers with a silent
hole in it. `0` on this screen always means zero; it never means "that query
did not answer". A partial-success shape would have to be typed, rendered
and reasoned about at every call site, and the honest failure is cheaper
than a page an operator has to learn to distrust.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.clock import Clock
from app.modules.admin.application.ports import (
    AuditEntryFilters,
    AuditEntryRepository,
    SanctionRepository,
)
from app.modules.admin.domain.audit import AuditEntry
from app.modules.game.public import AdministrativeMatchDirectory
from app.modules.game.public.administration import AdminLiveMatchSummary
from app.modules.notifications.public import (
    AdminDeliveryHealth,
    AdministrativeNotificationDirectory,
)
from app.modules.tournament.public.administration import (
    AdministrativeTournamentDirectory,
    AdminLiveTournamentSummary,
)
from app.modules.users.public import AdministrativeUserDirectory
from app.modules.users.public.administration import AdminAccountSummary

#: The two windows the accounts card reports.
#:
#: A day and a week, because those are the two questions an operator asks
#: about registration — "is it happening today" and "is today unusual". A
#: configurable range would turn a dashboard into an analytics tool, which
#: `specs/admin.md` §6.14 rules out, and would make the query's cost the
#: caller's choice.
DAY = timedelta(days=1)
WEEK = timedelta(days=7)

#: How many audit entries the activity list carries.
#:
#: Ten: enough to see what has been happening, few enough that the read is a
#: bounded index scan and the page does not become the audit log. `/audit` is
#: one click away and is the surface for actually reading the trail.
RECENT_AUDIT_LIMIT = 10


@dataclass(frozen=True, slots=True)
class DashboardOverview:
    """Everything the operator's first screen shows.

    Each field is the owning module's own value object, unchanged. The
    dashboard does not define a flattened DTO of its own here because that
    would be a second place for a fact's shape to live — the presentation
    schema flattens once, at the boundary, where it is serialised.
    """

    accounts: AdminAccountSummary
    matches: AdminLiveMatchSummary
    tournaments: AdminLiveTournamentSummary
    restrictions_in_force: int
    deliveries: AdminDeliveryHealth
    recent_activity: Sequence[AuditEntry]
    generated_at: datetime
    """When the server composed this. Sent so the console can say how old
    the numbers are rather than implying they are live — nothing here
    streams, and a page with no timestamp invites an operator to trust a
    figure from twenty minutes ago."""


class DashboardService:
    """Reads the operator overview. **No write exists on it.**"""

    def __init__(
        self,
        *,
        accounts: AdministrativeUserDirectory,
        matches: AdministrativeMatchDirectory,
        tournaments: AdministrativeTournamentDirectory,
        sanctions: SanctionRepository,
        notifications: AdministrativeNotificationDirectory,
        audit: AuditEntryRepository,
        clock: Clock,
    ) -> None:
        self._accounts = accounts
        self._matches = matches
        self._tournaments = tournaments
        self._sanctions = sanctions
        self._notifications = notifications
        self._audit = audit
        self._clock = clock

    async def overview(self) -> DashboardOverview:
        """The whole screen, in seven reads.

        Sequential rather than gathered: they share one database session,
        which is not safe to use concurrently, and the alternative — a
        session per card — would open six connections to render one page.
        The reads are all indexed lookups over small sets, so the saving
        would be latency this surface does not need.
        """
        now = self._clock.now()

        accounts = await self._accounts.account_summary(since_day=now - DAY, since_week=now - WEEK)
        matches = await self._matches.live_match_summary()
        tournaments = await self._tournaments.live_tournament_summary()
        restrictions = await self._sanctions.count_effective(at=now)
        deliveries = await self._notifications.delivery_health()
        recent = await self._audit.page(
            filters=AuditEntryFilters(), limit=RECENT_AUDIT_LIMIT, cursor=None
        )

        return DashboardOverview(
            accounts=accounts,
            matches=matches,
            tournaments=tournaments,
            restrictions_in_force=restrictions,
            deliveries=deliveries,
            recent_activity=recent.entries,
            generated_at=now,
        )


__all__ = ["DAY", "RECENT_AUDIT_LIMIT", "WEEK", "DashboardOverview", "DashboardService"]
