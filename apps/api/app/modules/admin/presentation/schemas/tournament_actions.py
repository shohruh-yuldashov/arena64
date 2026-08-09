"""What the admin tournament commands accept — A64-024.5H.

## Semantic commands, never a status field

There is no `status` on any model here and no `PATCH`. A caller cannot ask
for `completed`, cannot ask for `cancelled`, and cannot name a transition
the aggregate's table forbids — because the transition is the **route**,
and the only thing a body carries is configuration for the one command that
needs any.

Three of the four commands therefore have **no request model at all**:
opening registration, closing it and starting take an id from the path and
nothing else.

## What creation does not accept

No id, no status, no `created_by`, no `format`. The first two are the
server's, the third is the administrator the guard resolved — the one field
where a client-supplied value would have been plausible and wrong — and the
fourth has exactly one legal value in v0.x, so a parameter for it would be
a field whose only valid answer is the default.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.domain.tournament import (
    MAX_CAPACITY,
    MIN_CAPACITY,
    TournamentStatus,
)

#: The longest name an operator may give a tournament.
#:
#: Bounded because it is rendered in a console, in a notification and in a
#: bracket header, and an unbounded string on an administrative form is
#: where a pasted paragraph ends up.
MAX_NAME_LENGTH = 120


class CreateTournamentRequest(BaseModel):
    """Everything a tournament needs, and nothing the server decides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    variant: ProductVariant
    speed_class: SpeedClass
    capacity: int = Field(
        ge=MIN_CAPACITY,
        le=MAX_CAPACITY,
        description="Bounds are the aggregate's own — a value outside them is "
        "refused by `Tournament.__post_init__` whatever this says.",
    )
    rated: bool = True
    registration_deadline: datetime | None = Field(
        default=None,
        description="When registration closes on its own. `null` means it closes "
        "only when somebody closes it.",
    )


class TournamentActionResponse(BaseModel):
    """What a command changed.

    Two facts and no aggregate: the console re-reads the tournament to
    render it, so returning a copy here would be a second shape to keep in
    step with the detail response.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tournament_id: UUID
    status: TournamentStatus
    matches_launched: int = Field(
        default=0,
        description="Non-zero only for `start`. A tournament that reached "
        "`in_progress` and launched nothing is a bracket that did not materialise.",
    )


__all__ = [
    "MAX_NAME_LENGTH",
    "CreateTournamentRequest",
    "TournamentActionResponse",
]
