"""What the admin Tournaments API returns — A64-024.5 §5, §19.

An explicit allowlist. No ORM object is serialised: the port hands back
frozen records of stored facts, and these models name the subset an
operator sees.

**Entrants carry a player id and a name and nothing else** (§9) — no email,
no profile, no registration token, no block state. The console links to
`/users/{id}` for anything the person's own page owns.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AdminTournamentSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tournament_id: UUID
    name: str
    format: str
    variant: str
    speed_class: str
    status: str
    rated: bool
    capacity: int
    entrant_count: int
    registration_deadline: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class AdminTournamentPageResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[AdminTournamentSummary]
    next_cursor: str | None = None


class AdminEntrantView(BaseModel):
    """One registration. Identity and registration state, nothing more."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    player_id: UUID
    username: str | None = None
    display_name: str | None = None
    status: str
    seed_number: int | None = None
    registered_at: datetime
    withdrawn_at: datetime | None = None


class AdminRoundView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    round_number: int
    status: str
    pairing_count: int
    published_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AdminPairingView(BaseModel):
    """One bracket node, with the coordinates the tree is derived from.

    `round_number` and `slot` are the node's identity. Its parent is
    `(round_number + 1, slot // 2)` — the same arithmetic
    `tournament.domain.bracket_plan` uses, published rather than restated,
    so a console cannot draw a tree that disagrees with the domain's.

    `match_ids` is plural because a pairing may be replayed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    round_number: int
    slot: int
    light_player_id: UUID | None = None
    dark_player_id: UUID | None = None
    light_seed: int | None = None
    dark_seed: int | None = None
    winner_id: UUID | None = None
    advancement_reason: str | None = None
    match_ids: list[UUID] = Field(default_factory=list)


class AdminStandingView(BaseModel):
    """One final placement, **as `tournament` computed it** — never
    recomputed here (§13)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    player_id: UUID
    username: str | None = None
    display_name: str | None = None
    final_rank: int
    seed_number: int
    elimination_round: int | None = None
    eliminated_by_player_id: UUID | None = None
    wins: int
    losses: int
    draws: int
    final_status: str


class AdminTournamentDetailResponse(BaseModel):
    """One tournament and everything bounded by its capacity — §5.

    One response rather than four endpoints: entrants, rounds and pairings
    are all O(capacity), so fetching them together costs a fixed number of
    statements and saves the console three round trips.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tournament: AdminTournamentSummary
    entrants: list[AdminEntrantView]
    rounds: list[AdminRoundView]
    pairings: list[AdminPairingView]
    standings: list[AdminStandingView]


__all__ = [
    "AdminEntrantView",
    "AdminPairingView",
    "AdminRoundView",
    "AdminStandingView",
    "AdminTournamentDetailResponse",
    "AdminTournamentPageResponse",
    "AdminTournamentSummary",
]
