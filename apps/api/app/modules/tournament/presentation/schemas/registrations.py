"""Wire shapes for entering and leaving a tournament — A64-019.8 §2.

One response model, and **no request model at all**: the two participant
endpoints take everything they need from the path and from the
authenticated identity. There is deliberately no `player_id` field anywhere
in this file — a body that could carry one is a body somebody will
eventually populate with somebody else's id, and an absent field is a
guarantee where a validated one is only a check that could be forgotten.

## What a registration is allowed to say

Only what the player already knows: which tournament, which player, what
state their entry is in, when it happened, and what the tournament is
doing. `seed_number` appears once the field has been seeded, because a
player who has been drawn is entitled to see where.

Withheld: the no-show deadline and the attendance instants (a live policy
in flight), every compare-and-set target, and every ORM row. A registration
is a player's own record, not a window into the machinery around it.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.tournament.application.read_models import RegistrationDetail


class RegistrationResponse(BaseModel):
    """One player's entry in one tournament.

    Returned by both participant endpoints, so a client that entered and a
    client that withdrew read the same shape and branch on `status` rather
    than on which call they made.
    """

    tournament_id: UUID
    player_id: UUID
    status: str = Field(description="registered or withdrawn.")
    registered_at: datetime
    withdrawn_at: datetime | None = None

    seed_number: int | None = Field(
        default=None,
        description="This entrant's seed, once the tournament has been seeded.",
    )
    tournament_status: str = Field(
        description="The tournament's own lifecycle state, so a client needs one call."
    )

    @classmethod
    def of(cls, detail: RegistrationDetail) -> "RegistrationResponse":
        return cls(
            tournament_id=detail.tournament_id,
            player_id=detail.player_id,
            status=detail.status.value,
            registered_at=detail.registered_at,
            withdrawn_at=detail.withdrawn_at,
            seed_number=detail.seed_number,
            tournament_status=detail.tournament_status.value,
        )


__all__ = ["RegistrationResponse"]
