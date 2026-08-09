"""What an administrator may read about matches — A64-024.4.

A **separate published port** from `MatchHistoryReader`, for the reason
`users.public.administration` is separate from the player search: they
answer different questions.

`history_for` is scoped to **one player** and to **finished** matches,
because that is what a profile renders. An operator investigating an
incident has neither constraint: they start from a match id or a status,
and the match they care about is very often the one still being played.

## Read-only, and structurally so

There is no write on this port. Not a result, not a status, not a
cancellation — A64-024.4 is read-only because `admin.audit_entry` is
unbuilt (`specs/admin.md` §7), and an unaudited match mutation is the one
thing that must not be reachable from a console. Nothing here could perform
one even if a route asked.

## Why the record carries stored facts only

Every field below is read from the match row. Nothing is derived by
replaying the game, which is what lets a list of fifty matches cost one
query rather than fifty engine runs — the same reasoning
`MatchHistoryEntry` records, applied to a wider selection.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.engine import PlayerSide
from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import MatchOrigin, ProductVariant
from app.modules.game.public.matches import MatchTimeControl


@dataclass(frozen=True, slots=True)
class AdminMatchRecord:
    """One match, as an operator sees it.

    Primitive-only and stored-facts-only. Deliberately **without** the
    board, the move log, the clock deadlines, the queue ticket ids and the
    draw-offer bookkeeping: a list row needs none of them, and a port that
    carried them would make every page pay for the widest reader.
    """

    match_id: UUID
    status: MatchRecordStatus
    variant: ProductVariant
    rated: bool
    origin: MatchOrigin
    """How the pairing came about — queue, challenge, rematch or tournament.

    The single most useful field on this record for an operator, and the
    reason it is here rather than derived: "was this a tournament game" is
    the first question asked about a disputed result, and it is a column.
    """

    light_player_id: UUID
    dark_player_id: UUID

    outcome: MatchOutcome | None
    termination_reason: TerminationReason | None
    winner: PlayerSide | None

    time_control: MatchTimeControl | None
    speed_class: str | None
    ply_number: int

    created_at: datetime
    settled_at: datetime | None
    """When the acceptance handshake ended — both accepted, or somebody
    declined, or the window closed.

    **Not a "started_at"**: there is no such column, and inventing one from
    `clock_turn_started_at` would be a clock field wearing a lifecycle
    name. `settled_at` is the real fact the schema stores, and
    `ck_match__settled_at_iff_not_pending` is what keeps it honest."""

    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdminMatchFilters:
    """What an operator may narrow by.

    Every member maps to a column on the match row, so each is a `WHERE`
    the planner can use rather than a post-filter. `None` means "either".

    **Deliberately absent: participant username.** It lives in another
    schema, so filtering by it would mean a cross-schema join (DB-03
    forbids) or resolving names to ids first — which is a second query the
    caller can make itself, and does. `participant_id` is the port's form
    of that question.
    """

    status: MatchRecordStatus | None = None
    rated: bool | None = None
    variant: ProductVariant | None = None
    origin: MatchOrigin | None = None
    participant_id: UUID | None = None
    """Matches either seat. Backed by the two partial indexes on
    `(player_id, created_at)` for live matches, and by the primary key's
    ordering otherwise."""


@dataclass(frozen=True, slots=True)
class AdminMatchPage:
    """One page, and the cursor that continues it.

    No total count, for the reason `AdminUserPage` has none: an operator
    needs "are there more", and counting a partitioned match table is a
    scan per page.
    """

    records: Sequence[AdminMatchRecord]
    next_cursor: str | None


class AdministrativeMatchDirectory(Protocol):
    """Reads matches for the admin console. **No write exists.**"""

    async def list_matches(
        self, *, filters: AdminMatchFilters, limit: int, cursor: str | None
    ) -> AdminMatchPage:
        """One page of matches, newest first.

        Ordered by `(created_at, id)` — the match table's **primary key**,
        so the ordering is total and the keyset is an index seek rather
        than a sort. `created_at` alone is not unique and a cursor on it
        would silently skip or repeat rows.

        Bounded by `limit` always. There is no unbounded form and no query
        language: a caller supplies typed filters and a cursor, and can
        express nothing else.
        """
        ...

    async def find_match(self, match_id: UUID) -> AdminMatchRecord | None:
        """One match, or `None`. Every status, not only finished ones."""
        ...


__all__ = [
    "AdminMatchFilters",
    "AdminMatchPage",
    "AdminMatchRecord",
    "AdministrativeMatchDirectory",
]
