"""What the admin Matches API returns — A64-024.4 §5, §9, §11.

**An explicit allowlist.** No ORM object is serialised anywhere on this
path: the port returns a frozen record of stored facts, and these models
name the subset an operator sees.

Deliberately absent, and unreachable through the port beneath: queue ticket
ids, clock deadlines, draw-offer bookkeeping, the board, the move log,
and every participant field except an id and a display name — no email, no
IP, no device, no session, no token (§11).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AdminMatchParticipant(BaseModel):
    """One seat.

    **The minimum operator-safe identity**: who it was and what to call
    them. §11 is explicit that a match page does not need an email, and the
    console links to `/users/$userId` for anything more — which is a page
    with its own guard and its own decision about what to show.

    `username` is `None` when the account no longer resolves. That is a real
    state — an erased participant — rather than a gap, and the console
    renders the id it already has.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    player_id: UUID
    username: str | None = None
    display_name: str | None = None
    side: str


class AdminMatchSummary(BaseModel):
    """One match in the list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    match_id: UUID
    status: str
    variant: str
    rated: bool
    origin: str
    light: AdminMatchParticipant
    dark: AdminMatchParticipant
    outcome: str | None = None
    winner: str | None = None
    termination_reason: str | None = None
    speed_class: str | None = None
    ply_number: int
    created_at: datetime
    ended_at: datetime | None = None


class AdminMatchPageResponse(BaseModel):
    """One page, and the cursor that continues it. No total count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[AdminMatchSummary]
    next_cursor: str | None = None


class AdminMatchTimeControl(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_ms: int
    increment_ms: int


class AdminMatchDetail(AdminMatchSummary):
    """One match in full — §9.

    Adds only what a list row omits: the handshake instant, the engine
    version the game was played under, and the time control.

    **No move list.** The replay read is a separate, more expensive port
    (`MatchReplayReader` applies every ply through the engine), and §10 asks
    for it only where the architecture supports it naturally. It is not
    folded in here so that opening a match detail does not replay a game.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    settled_at: datetime | None = Field(
        default=None,
        description="When the acceptance handshake ended — not a start time.",
    )
    time_control: AdminMatchTimeControl | None = None


__all__ = [
    "AdminMatchDetail",
    "AdminMatchPageResponse",
    "AdminMatchParticipant",
    "AdminMatchSummary",
    "AdminMatchTimeControl",
]
