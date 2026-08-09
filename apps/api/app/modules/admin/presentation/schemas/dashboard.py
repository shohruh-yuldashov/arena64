"""What the admin dashboard returns — A64-024.9.

Flat, small, and **less than any destination page**. Every card is a number
and a label; the detail behind it lives on the console the card links to,
which is where the decision about what may be shown was already made.

## No percentages, no trends, no deltas

Nothing here compares two periods, because nothing stores a prior period. A
"+12%" composed from two numbers the platform never recorded together would
be a figure an operator acts on and cannot reproduce.

## No health block

`/health/ready` exists and pings PostgreSQL and every Redis pool. Putting it
on this response would make one operator opening a browser tab into a probe
across every infrastructure dependency, on every page load. Liveness belongs
to the orchestrator that already asks for it.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AccountsCard(BaseModel):
    """Registrations, in two bounded windows. **No total.**"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registered_last_day: int
    registered_last_week: int


class MatchesCard(BaseModel):
    """Games in flight right now."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    active: int
    awaiting_acceptance: int = Field(
        description="Pairings offered and not yet taken up by both players."
    )


class TournamentsCard(BaseModel):
    """The two tournament states an operator can act on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registration_open: int
    in_progress: int


class AttentionCard(BaseModel):
    """Things waiting for a person.

    Both members are states the product itself defines as actionable — not
    thresholds invented here. A restriction in force is somebody currently
    unable to sign in; `retry_exhausted` is the one push state A64-024.7
    built an operator action for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    restrictions_in_force: int
    push_deliveries_retry_exhausted: int


class ActivityEntry(BaseModel):
    """One recent privileged action.

    The audit log's own facts, narrowed further: the action, who did it,
    what it was about, and when. **No `before`/`after` metadata** — the
    dashboard is a place to notice that something happened, and `/audit` is
    where a reviewer reads what changed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    action: str
    outcome: str
    actor_type: str
    actor_id: UUID | None = None
    actor_username: str | None = None
    subject_type: str
    subject_ref: str
    created_at: datetime


class DashboardResponse(BaseModel):
    """The operator's overview."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accounts: AccountsCard
    matches: MatchesCard
    tournaments: TournamentsCard
    attention: AttentionCard
    recent_activity: list[ActivityEntry]
    generated_at: datetime = Field(
        description="When the server composed this. Nothing here streams, so the "
        "console shows the age of the numbers rather than implying they are live."
    )


__all__ = [
    "AccountsCard",
    "ActivityEntry",
    "AttentionCard",
    "DashboardResponse",
    "MatchesCard",
    "TournamentsCard",
]
