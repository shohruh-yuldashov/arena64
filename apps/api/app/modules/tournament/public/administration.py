"""What an administrator may read about tournaments — A64-024.5.

A **separate published port**, for the reason `users` and `game` each have
one: the existing published reads answer narrow questions —
`TournamentNotificationReader` names an audience, `TournamentAttendance`
records who turned up — and neither describes a tournament to somebody
investigating it.

## Read-only, structurally

There is no write here. Not a status, not a round publication, not a
withdrawal, not a bracket edit. A64-024.5 is read-only because
`admin.audit_entry` is unbuilt (`specs/admin.md` §7), and a tournament
mutation is the most consequential unaudited write this platform could
offer — it moves brackets and therefore ratings.

## The bracket is real, and this port publishes it as such

`pairing` is keyed `(tournament_id, round_number, slot)` and the tree is
arithmetic: a node's parent is `(round_number + 1, slot // 2)`, even slots
feeding the light seat and odd the dark — `domain.bracket_plan` states it.

So the structure does not have to be inferred or invented. This port
publishes the nodes with their coordinates and lets a consumer derive the
edges from the same arithmetic the domain uses, rather than shipping a
second description of the tree that could disagree with it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.game.public.variants import ProductVariant
from app.modules.tournament.domain.registration import RegistrationStatus
from app.modules.tournament.domain.rounds import RoundStatus
from app.modules.tournament.domain.standings import FinalStatus
from app.modules.tournament.domain.tournament import TournamentFormat, TournamentStatus


@dataclass(frozen=True, slots=True)
class AdminTournamentRecord:
    """One tournament, as a list row.

    Stored facts only. `entrant_count` is the single derived value, and it
    is counted in the same statement as the page rather than per row — see
    the directory.
    """

    tournament_id: UUID
    name: str
    format: TournamentFormat
    variant: ProductVariant
    speed_class: str
    status: TournamentStatus
    rated: bool
    capacity: int
    entrant_count: int
    registration_deadline: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AdminLiveTournamentSummary:
    """The two tournament states an operator can act on — A64-024.9.

    A tournament taking entries has a deadline somebody may need to extend;
    one in progress has rounds that must be published. Everything else —
    draft, closed, completed, cancelled — is either not started or finished,
    and neither is an operational item.

    **This is the one dashboard read without an index behind it**, and it is
    deliberate rather than overlooked. `tournaments.tournament` holds one row
    per tournament ever created, and tournaments are created by operators
    rather than by traffic: the table grows by a handful of rows a day, not
    by a thousand. A sequential scan of it is measured in microseconds and
    stays that way for years.

    The threshold at which that stops being true is worth writing down: at
    roughly a hundred thousand rows the scan is no longer free, and the fix
    is one partial index on the two live statuses — the same shape
    `ix_match__current_*` already uses. Adding it now would be an index
    guessed at rather than measured, which `specs/admin.md` §6.14 records.
    """

    registration_open: int
    in_progress: int


@dataclass(frozen=True, slots=True)
class AdminTournamentFilters:
    """What an operator may narrow by — every member a column.

    **No name search.** `tournament.name` carries no index, so a substring
    match would be a sequential scan; an operator who has a name reaches
    the tournament through the list, which is short by nature. Deferred
    rather than added expensively.

    **No entrant filter.** Registrations live in another table and
    filtering by one would mean a join that breaks the keyset — the
    tournament a player entered is reachable from that player's matches
    instead.
    """

    status: TournamentStatus | None = None
    format: TournamentFormat | None = None
    variant: ProductVariant | None = None
    rated: bool | None = None


@dataclass(frozen=True, slots=True)
class AdminTournamentPage:
    records: Sequence[AdminTournamentRecord]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class AdminEntrant:
    """One registration.

    **No email, no profile, no block state** — §9. The player id is the
    platform's opaque reference, and the console links to `/users/{id}` for
    anything the person's own page owns.
    """

    player_id: UUID
    status: RegistrationStatus
    seed_number: int | None
    registered_at: datetime
    withdrawn_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdminRound:
    """One round of the bracket."""

    round_number: int
    status: RoundStatus
    published_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    pairing_count: int


@dataclass(frozen=True, slots=True)
class AdminPairing:
    """One node of the bracket, with its coordinates.

    `round_number` and `slot` are the node's identity, and the edges follow
    from them: the parent is `(round_number + 1, slot // 2)`. A consumer
    derives the tree from the same arithmetic `domain.bracket_plan` uses
    rather than from a second description that could drift.

    `match_ids` are the games this node produced — plural because a pairing
    may be replayed, which is what `pairing_attempt` models. Empty for a bye
    and for a node nobody has reached yet.
    """

    round_number: int
    slot: int
    light_player_id: UUID | None
    dark_player_id: UUID | None
    light_seed: int | None
    dark_seed: int | None
    winner_id: UUID | None
    advancement_reason: str | None
    match_ids: Sequence[UUID]


@dataclass(frozen=True, slots=True)
class AdminStanding:
    """One final placement, as `tournament` computed it.

    Read from `standing`, never recomputed. The module owns the standings
    authority and §13 forbids a second algorithm — a console that derived
    placements from matches would be a second source of truth for who won.
    """

    player_id: UUID
    final_rank: int
    seed_number: int
    elimination_round: int | None
    eliminated_by_player_id: UUID | None
    wins: int
    losses: int
    draws: int
    final_status: FinalStatus


@dataclass(frozen=True, slots=True)
class AdminTournamentDetail:
    """One tournament in full.

    **One response rather than four endpoints** (§5): a tournament is
    bounded by its capacity, so entrants, rounds and pairings are all
    O(capacity) and fetching them together costs a fixed number of
    statements. Splitting them would make the console issue four round
    trips to render one page.
    """

    tournament: AdminTournamentRecord
    entrants: Sequence[AdminEntrant]
    rounds: Sequence[AdminRound]
    pairings: Sequence[AdminPairing]
    standings: Sequence[AdminStanding]


class AdministrativeTournamentDirectory(Protocol):
    """Reads tournaments for the admin console. **No write exists.**"""

    async def list_tournaments(
        self, *, filters: AdminTournamentFilters, limit: int, cursor: str | None
    ) -> AdminTournamentPage:
        """One page, newest first, ordered by `(created_at, id)`.

        `created_at` alone is not unique, so the `id` tiebreak is what makes
        the keyset total rather than approximately ordered.
        """
        ...

    async def live_tournament_summary(self) -> AdminLiveTournamentSummary:
        """The two live counts, in **one** grouped statement."""
        ...

    async def find_tournament(self, tournament_id: UUID) -> AdminTournamentDetail | None:
        """One tournament and everything bounded by its capacity, or `None`."""
        ...


__all__ = [
    "AdminEntrant",
    "AdminLiveTournamentSummary",
    "AdminPairing",
    "AdminRound",
    "AdminStanding",
    "AdminTournamentDetail",
    "AdminTournamentFilters",
    "AdminTournamentPage",
    "AdminTournamentRecord",
    "AdministrativeTournamentDirectory",
]
