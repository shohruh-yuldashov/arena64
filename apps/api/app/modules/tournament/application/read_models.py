"""What a public reader may see — SPEC-TOURNAMENT §6g, A64-019.6 §9–§12.

Values, not rows. Every type here is built from this module's own domain
objects and carries nothing an ORM model does: no version column, no
compare-and-set target, no lock state, no audit row.

## What is deliberately withheld, and why each one

    pairing.winner_id as a CAS target   the *value* is published as the
                                        node's winner; the fact that
                                        advancement is a compare-and-set is
                                        an implementation detail, and a
                                        client that saw it would be reading
                                        a concurrency mechanism
    no_show_deadline, present_at        an attendance policy in flight. A
                                        spectator who could see whose
                                        deadline was closest would be
                                        watching a player, not a game
    processed_event, outbox rows        audit, and never a read model's

`match_id` **is** published, because a bracket is only useful if a reader
can follow a node to the game that decided it — and `game` already answers
for its own visibility on `/matches/{id}/replay`. What is published here is
the identifier, never the moves: reconstructing a replay is `game`'s, and
doing it here would be a second implementation of the rules.

## Why these are application values rather than schemas

`presentation/schemas` turns them into wire shapes. Keeping the two apart
means the endpoint's field names are a contract this layer does not decide,
so an API rename is not a change to what a tournament knows about itself.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.domain.attempts import (
    AdvancementReason,
    AttemptOutcome,
    AttemptStatus,
)
from app.modules.tournament.domain.registration import RegistrationStatus
from app.modules.tournament.domain.rounds import RoundStatus
from app.modules.tournament.domain.standings import FinalStatus
from app.modules.tournament.domain.tournament import (
    TournamentFormat,
    TournamentStatus,
)


@dataclass(frozen=True, slots=True)
class TournamentSummary:
    """One tournament, as a detail page and a history entry both render it.

    Public in v0.x (§7): a tournament, its bracket and its results are
    visible to anybody. `created_by` is **not** here — who opened a
    tournament is operational, and publishing it would leak which
    tournaments the platform ran itself.
    """

    id: UUID
    name: str
    format: TournamentFormat
    variant: str
    speed_class: str
    rated: bool
    capacity: int
    status: TournamentStatus

    entrant_count: int
    """Live registrations. The number a lobby renders as "12 / 16"."""

    current_round: int | None
    """The round being played, or `None` before the bracket exists and after
    the tournament finishes."""

    registration_deadline: datetime | None
    """When entries close on their own, or `None` for operator-closed —
    A64-020.0B.

    Published because a lobby that cannot say *when* a tournament closes is
    one a player misses. The **fact** is public; the sweep that acts on it is
    not — see `infrastructure/tasks.py`, which is what claims the column."""

    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class AttemptSummary:
    """One `game` match played for a node — §10.

    Carries the match id so a reader can follow a node to the game that
    decided it, and the seats **as played**: a rematch swaps them (§6c), so
    reading them off the pairing would show the wrong player moving first in
    the second game of a tie.
    """

    attempt_number: int
    match_id: UUID
    light_player_id: UUID
    dark_player_id: UUID
    status: AttemptStatus
    outcome: AttemptOutcome | None
    winner_id: UUID | None


@dataclass(frozen=True, slots=True)
class BracketNodeView:
    """One node of a published bracket — §10.

    `pairing_id` is published because it is the stable name a client uses to
    correlate a node across two reads, and because `game` already holds it
    as an opaque `origin_ref`. It carries no meaning a reader could exploit:
    the coordinates it does *not* encode are the whole point (§6c).
    """

    pairing_id: UUID
    round_number: int
    slot: int

    light_player_id: UUID | None
    dark_player_id: UUID | None
    light_seed: int | None
    dark_seed: int | None

    winner_id: UUID | None
    advancement_reason: AdvancementReason | None
    attempts: tuple[AttemptSummary, ...]

    @property
    def is_bye(self) -> bool:
        """Whether this node advanced **without an opponent** — §6.

        Read off `advancement_reason`, which is the durable record of *why*
        somebody advanced, rather than counted from the seats.

        Counting seats is what `BracketSlot.is_bye` does, and it is correct
        there only because `propagated` pairs it with `_children_settled`:
        one participant means a bye **when nothing beneath can still deliver
        the other**. A read model has no such pairing, so counting alone
        reported a semi-final holding one player as a bye for the whole time
        the other semi-final was being played — the exact case
        `_children_settled` exists to exclude, published as a fact.

        Nothing else sets `BYE`, and both writers resolve byes in the same
        transaction that creates or advances the node, so no reader observes
        a genuine bye before its reason is recorded.
        """
        return self.advancement_reason is AdvancementReason.BYE


@dataclass(frozen=True, slots=True)
class RoundView:
    """One layer of the bracket, and where it stands."""

    round_number: int
    status: RoundStatus
    nodes: tuple[BracketNodeView, ...]


@dataclass(frozen=True, slots=True)
class BracketView:
    """A whole bracket, round by round — §10."""

    tournament_id: UUID
    rounds: tuple[RoundView, ...]


@dataclass(frozen=True, slots=True)
class StandingView:
    """One entrant's published result — §11."""

    player_id: UUID
    final_rank: int
    seed_number: int
    wins: int
    losses: int
    draws: int
    adjudicated_advancements: int
    final_status: FinalStatus
    elimination_round: int | None
    eliminated_by_player_id: UUID | None


@dataclass(frozen=True, slots=True)
class PlayerTournamentEntry:
    """One tournament a player entered — §12.

    The summary a personal history renders, plus the two facts that only
    exist once the tournament finished. Both are `None` while it is running,
    which is what tells a client to render "in progress" rather than a
    placing.
    """

    tournament: TournamentSummary
    seed_number: int | None
    final_rank: int | None
    final_status: FinalStatus | None


@dataclass(frozen=True, slots=True)
class RegistrationDetail:
    """One player's own entry, as the participant endpoints return it — §2.

    Read back after the write rather than assembled from what the service
    happened to know, and in **one** statement: the seed and the
    tournament's status both live outside the `Registration` value, and
    inferring them from "we just registered, so it must be open and
    unseeded" would be a response that is right only until the endpoint is
    reused.
    """

    tournament_id: UUID
    player_id: UUID
    status: RegistrationStatus
    registered_at: datetime
    withdrawn_at: datetime | None
    seed_number: int | None
    tournament_status: TournamentStatus


@dataclass(frozen=True, slots=True)
class TournamentFilter:
    """Which tournaments a lobby page is asking for — A64-020.0B.

    A **closed** set of five, and that is the contract rather than a
    starting point. Each one is an enum or a boolean the tournament already
    stores, so every combination is a predicate over indexed columns and no
    filter can be expensive.

    What is deliberately absent: a creator filter (`created_by` is
    operational and is not published at all), a region, a prize, a
    private/public flag — private tournaments do not exist in v0.x (§7) —
    free text, and any caller-chosen ordering. Each of those is either a
    product decision nobody has made or an unbounded scan, and a list
    endpoint that accepts an arbitrary sort is one whose cost its author
    cannot state.
    """

    status: TournamentStatus | None = None
    format: TournamentFormat | None = None
    variant: ProductVariant | None = None
    speed_class: SpeedClass | None = None
    rated: bool | None = None

    @property
    def is_empty(self) -> bool:
        """Whether this asks for everything — the lobby's default.

        Completed and cancelled tournaments are **included** by default.
        A lobby that hid them would answer "what happened here?" with
        silence, and the status filter is what narrows the view when a
        client wants only what it can enter.
        """
        return not any(
            value is not None
            for value in (self.status, self.format, self.variant, self.speed_class, self.rated)
        )


@dataclass(frozen=True, slots=True)
class TournamentListCursor:
    """Where a lobby page resumes — A64-020.0B.

    `(created_at, tournament_id)`, descending. The second key makes the
    order **total**: two tournaments created in the same millisecond would
    otherwise page unstably, which on a keyset is a row seen twice or not at
    all. The same shape `HistoryCursor` has, over the tournament's own
    instant rather than a registration's.
    """

    created_at: datetime
    tournament_id: UUID


@dataclass(frozen=True, slots=True)
class TournamentPage:
    """One page of the lobby, newest first — A64-020.0B."""

    entries: tuple[TournamentSummary, ...]
    next_cursor: TournamentListCursor | None
    """`None` on the last page. Keyset rather than `OFFSET`: the lobby grows
    without bound, and an offset scan costs more with every page and shifts
    the moment a tournament is created mid-walk."""


@dataclass(frozen=True, slots=True)
class HistoryCursor:
    """Where a player's tournament history resumes — §12.

    `(registered_at, tournament_id)`, descending. The second key is what
    makes the order **total**: two tournaments entered in the same
    millisecond would otherwise page unstably, which on a keyset is a row
    seen twice or not at all.
    """

    registered_at: datetime
    tournament_id: UUID


@dataclass(frozen=True, slots=True)
class PlayerTournamentPage:
    """One page of a player's tournament history."""

    entries: tuple[PlayerTournamentEntry, ...]
    next_cursor: HistoryCursor | None
    """`None` on the last page. Keyset rather than `OFFSET`, because §12's
    history is unbounded and an offset scan grows with the page number."""


__all__ = [
    "AttemptSummary",
    "BracketNodeView",
    "BracketView",
    "HistoryCursor",
    "PlayerTournamentEntry",
    "PlayerTournamentPage",
    "RegistrationDetail",
    "RoundView",
    "StandingView",
    "TournamentFilter",
    "TournamentListCursor",
    "TournamentPage",
    "TournamentSummary",
]
