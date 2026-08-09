"""What the admin Moderation API accepts and returns — A64-024.6.

## The request carries a decision, never an actor

`RestrictAccountRequest` has no actor field and no target field. The actor
is the account `CurrentAdmin` resolved and the target is the path
parameter, so there is nothing a caller could send that changes who did
what to whom. `extra="forbid"` closes the rest: a payload naming a field
this model does not declare is a `422`, not a silently ignored key.

## Duration, not an instant

The client states **how long**, and the server computes the expiry against
its own clock. An absolute `expires_at` from a browser is a value subject
to the operator's device clock, and a skewed one produces a restriction
that ends earlier or later than the person applying it intended — silently,
because nothing would look wrong.

`None` means indefinite, which is not permanent: a restore ends it.

## What the read surface withholds

`reasoning` is on the detail of a restriction an administrator opened, and
nowhere near a player-facing route. Nothing in this module is reachable
without `CurrentAdmin`, and no field here could carry a credential, a
session or an address — see the tests, which assert the absence.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.admin.domain.moderation import (
    MAX_REASONING_LENGTH,
    ModerationCategory,
    SanctionKind,
)

#: The longest restriction an operator may set in one action — one year.
#:
#: Bounded because an unbounded hour count is an off-by-a-zero away from a
#: restriction that outlives the platform, and because "indefinite" already
#: has a spelling: omit the field. A longer restriction is a second
#: deliberate action, which is the correct amount of friction.
MAX_DURATION_HOURS = 24 * 365


class RestrictAccountRequest(BaseModel):
    """Withhold access from an account."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: ModerationCategory = Field(
        description="Why, from a closed vocabulary. The console localises it; "
        "the server stores the identifier."
    )
    reasoning: str = Field(
        min_length=1,
        max_length=MAX_REASONING_LENGTH,
        description="The decision's reasoning, recorded on the moderation case. "
        "Plain text, bounded, and never shown to the restricted account.",
    )
    duration_hours: int | None = Field(
        default=None,
        ge=1,
        le=MAX_DURATION_HOURS,
        description="How long the restriction lasts. Omit for an indefinite one.",
    )


class ModerationCaseView(BaseModel):
    """The decision behind a restriction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    category: str
    decision: str
    reasoning: str
    opened_by: UUID
    opened_by_username: str | None = Field(
        default=None,
        description="Resolved per page in one batch. `None` for an account that "
        "no longer exists — the case outlives it.",
    )
    opened_at: datetime


class SanctionView(BaseModel):
    """One restriction, with the decision that authorised it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    player_id: UUID
    username: str | None = None
    kind: SanctionKind
    is_effective: bool = Field(
        description="Whether it is in force **now**. Computed at read time from "
        "`expires_at` and `lifted_at` — no job removes anything."
    )
    starts_at: datetime
    expires_at: datetime | None = Field(
        default=None, description="`None` for an indefinite restriction."
    )
    lifted_at: datetime | None = None
    lifted_by: UUID | None = None
    case: ModerationCaseView


class SanctionPageResponse(BaseModel):
    """One page, and the cursor that continues it.

    **No total count**, for the reason no other admin page has one: an
    operator needs "are there more".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[SanctionView]
    next_cursor: str | None = None


class AccountModerationState(BaseModel):
    """One account's moderation standing, for the user detail page.

    `restriction` is the **effective** one or `None`. A lifted or expired
    restriction is history and belongs to `/moderation`, not to the badge
    that tells an operator whether this person can sign in right now.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_restricted: bool
    restriction: SanctionView | None = None


__all__ = [
    "MAX_DURATION_HOURS",
    "AccountModerationState",
    "ModerationCaseView",
    "RestrictAccountRequest",
    "SanctionPageResponse",
    "SanctionView",
]
