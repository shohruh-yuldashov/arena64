"""`QueueTicket` — one player's standing request to be paired.

domain-model.md §10.2, and the aggregate root architecture.md §6 assigns to
`matchmaking`. Framework-free (architecture.md §8): no SQL, no Redis, no
clock — time arrives as an argument (AD-07).

## The five states, and the two this task does not implement

A64-014.1 specified four — `waiting`, `matched`, `cancelled`, `expired` —
and A64-015.3 adds the fifth, `reserved`, because pairing needs it.
domain-model.md §10.2's diagram has seven, and the difference is worth
recording rather than resolving silently, because the missing two are
*future* states and not omissions:

    Queued      -> `waiting`
    Widening    -> not modelled. QT-5's widening rating window is a
                   property of a *pairing scan*, not of a ticket: the
                   ticket carries `entered_at` and the scan derives the
                   window from its age. A state whose only content is "the
                   scan has looked at this a few times" is state the scan
                   can recompute, and one more transition to get wrong.
    Reserved    -> `reserved` (A64-015.3). See below.
    Consumed    -> `matched`. The name follows A64-014.1 rather than
                   domain-model.md; both mean "this ticket produced a
                   match and is finished".
    Cancelled   -> `cancelled`
    Expired     -> `expired`
    Abandoned   -> not modelled. It is `expired` with a different cause,
                   and the cause is only knowable once presence is watched
                   continuously rather than checked at entry.

## Why `reserved` had to exist — A64-015.3

A64-015.1 predicted this state and A64-015.3 is where the prediction pays.
Pairing is two steps that cannot be one: claim both tickets, then ask
`game` to create a match. They cannot share a transaction, because a
cross-context call inside an open transaction is what services.md BE-05
forbids — it would hold two row locks across another module's work, and
nobody could reason about the lock-acquisition order.

So there is a window between "these two are mine" and "a match exists",
and a status is what makes that window visible to every other worker:

    waiting -> reserved     a worker has taken this pair
    reserved -> matched     `game` accepted the match request
    reserved -> waiting     it did not, and the player goes back in line

Marking a ticket `matched` at the claim would be the alternative, and it is
the one A64-015.3 §8 forbids by name: a ticket that says it produced a
match before any match exists is a lie that survives a crash.

**`reserved` is live, not terminal.** It carries no `resolved_at`, it is
covered by QT-1's uniqueness index, and `active_ticket` reports it — a
player being paired is still queued, and must not be able to join a second
pool while a worker is creating their match.

**A reservation has its own, much shorter deadline — A64-015.4.** A64-015.3
left a crashed worker's two reserved tickets to the ordinary ten-minute
expiry sweep, which meant two players stood in a queue that had already
stopped considering them for the rest of their window. `reserved_until`
closes that: it is written at the claim, it is the instant the reconciler
acts on, and it is **the same value** the match carries as its
`acceptance_deadline`. One number, in two rows, read by both halves of the
handshake — see `PairingService._claim` and
`game.domain.match_record`.

**A released ticket keeps its `entered_at`.** Compensation restores the
row's status and nothing else, so a player whose match creation failed
returns to exactly the place in line they held — not to the back of it for
a failure that was the platform's.

## Why every transition returns a new ticket

Frozen, like `Block` and `OutboxEntry` and unlike `Friendship`. Two
reasons, and the second is the one that decided it:

  - There is no in-place edit here. A ticket is entered once and resolved
    once; nothing is ever amended, so a mutable aggregate would offer a
    setter for a transition that does not exist.
  - The write that persists a resolution is a **compare-and-set** on
    `status` (see `QueueRepository.resolve`), which needs the before and
    the after as two values. A mutating `cancel()` would leave the caller
    holding only the after, and the repository would have to trust that
    the row still says what it said when it was read — which under two
    devices cancelling at once it does not.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.matchmaking.domain.exceptions import TicketNotWaiting
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType, Region

#: The rating a ticket records when nothing has measured the player.
#:
#: A second copy of `profiles.domain.ratings.STARTING_RATING`, and that is
#: a deliberate, recorded duplication rather than an oversight. R-1 forbids
#: importing another module's `domain`, and `profiles` publishes no rating
#: port — because a public *profile*'s starting value and a *matchmaker*'s
#: are only the same number by coincidence today.
#:
#: Both are placeholders for the `rating` module, which owns the figure
#: (domain-model.md §24). When it exists, `RatingSnapshotProvider` is
#: satisfied by `rating.public` and this constant is deleted; until then a
#: grep for `1500` finds both places that assumed it, which is the property
#: that matters.
PROVISIONAL_RATING = 1500


class QueueStatus(StrEnum):
    """A ticket's position in its lifecycle — see this module's docstring
    for the mapping to domain-model.md §10.2's seven.

    Two of these are **live** and three are terminal, and nearly every rule
    in this module is a statement about that split: a live ticket occupies
    QT-1's uniqueness, carries no `resolved_at`, and is reported to its
    owner as "you are queued". A terminal one is finished and immutable.

    The two live states differ in one way that matters: a **pairing scan
    reads only `waiting`**, so a reserved ticket is invisible to every
    other worker's next scan while it is being turned into a match.
    """

    WAITING = "waiting"
    RESERVED = "reserved"
    """A pairing worker has claimed this ticket and is creating its match —
    A64-015.3. Live, not terminal, and not scannable."""

    MATCHED = "matched"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_live(self) -> bool:
        """Whether the player is still in the queue as far as every rule on
        this platform is concerned."""
        return self in _LIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return not self.is_live


#: The two live statuses, as one set.
#:
#: Defined once and mirrored by three database predicates —
#: `uq_queue_ticket__one_live_per_player`, `ix_queue_ticket__due` and
#: `ck_queue_ticket__resolved_iff_terminal`. A grep for this name finds
#: every place the split is asserted, which is the property that matters
#: when a sixth status is added.
_LIVE_STATUSES = frozenset({QueueStatus.WAITING, QueueStatus.RESERVED})


@dataclass(frozen=True, slots=True)
class QueueTicket:
    """One player waiting for a match.

    Frozen — see this module's docstring on why a transition returns a new
    instance rather than mutating this one.
    """

    player_id: UUID
    """Whose ticket it is. An opaque cross-context identifier (DM-06): no
    foreign key, and nothing here can resolve it to a person."""

    pool: QueuePool
    """Which queue this ticket is waiting in — A64-015.2.

    One value rather than the `queue_type` and `region` pair A64-015.1
    carried, and now carrying the variant as well. `QueuePool` says why the
    three belong together; the short version is that they are what decides
    whether two players are candidates for each other.

    `queue_type` and `region` remain readable as properties below, because
    the columns, the indexes and the pool scan all name them individually
    and a repository should not have to reach through two dots to build a
    `WHERE`.
    """

    rating_snapshot: int
    """The player's rating **at entry** — QT-2.

    Not a reference to a live rating, and the difference is a correctness
    one rather than a caching one: "pairing must be deterministic within a
    tick; a rating changing mid-scan would make the same scan pair
    inconsistently." A player whose rating moves while they wait keeps the
    number they queued with until they re-queue.
    """

    entered_at: datetime
    """When the player joined the pool. The pairing order (oldest first)
    and the input to QT-5's widening window."""

    expires_at: datetime
    """When the ticket stops being a claim on anybody's attention.

    Absolute rather than a duration, so a ticket written under one
    `MATCHMAKING_TICKET_TTL_SECONDS` is not silently re-dated by a deploy
    that changes it.
    """

    status: QueueStatus = QueueStatus.WAITING

    resolved_at: datetime | None = None
    """When the ticket left `waiting`, and `None` while it has not.

    Set exactly when `status` is terminal — a database CHECK enforces the
    same pairing (BE-06), so a row cannot claim an outcome without its
    instant.
    """

    reserved_until: datetime | None = None
    """How long this reservation may stand before it is reconciled —
    A64-015.4 §5.

    Set exactly when `status` is `reserved`, and a database CHECK enforces
    the same pairing. Absolute rather than a duration, for the reason
    `expires_at` is: a reservation written under one
    `MATCHMAKING_RESERVATION_TTL_SECONDS` must not be silently re-dated by
    a deploy that changes it.

    **Much shorter than `expires_at`**, and a settings validator enforces
    that too. The two answer different questions — "how long will this
    player wait for an opponent" and "how long may a worker hold their
    ticket while creating a match" — and the second is measured in seconds
    because it is a crash-recovery grace rather than a wait.
    """

    id: UUID = field(default_factory=generate_uuid7)
    """UUIDv7, application-generated (DB-07). Last so every other field can
    be passed positionally by the repository's rehydration."""

    def __post_init__(self) -> None:
        # Re-checked here rather than only in `enter`, because the
        # repository constructs instances directly when rehydrating — this
        # is what makes a corrupt row fail at the boundary rather than
        # reach a response. The database's own CHECK constraints are the
        # authoritative copies (BE-06).
        if self.expires_at <= self.entered_at:
            raise ValueError("a queue ticket cannot expire before it was entered")
        if self.rating_snapshot < 0:
            raise ValueError("rating_snapshot cannot be negative")
        if self.status.is_terminal != (self.resolved_at is not None):
            raise ValueError("resolved_at is set exactly when the ticket is no longer live")
        if (self.status is QueueStatus.RESERVED) != (self.reserved_until is not None):
            raise ValueError("reserved_until is set exactly when the ticket is reserved")

    @property
    def queue_type(self) -> QueueType:
        """This ticket's pool mode. Read through the pool, never stored
        twice — two copies of one fact is two things to keep in step."""
        return self.pool.queue_type

    @property
    def region(self) -> Region:
        """This ticket's pool region."""
        return self.pool.region

    @classmethod
    def enter(
        cls,
        *,
        player_id: UUID,
        pool: QueuePool,
        rating_snapshot: int,
        at: datetime,
        ttl: float,
    ) -> "QueueTicket":
        """A new waiting ticket.

        Enforces only what this aggregate can see on its own — that the
        window is positive and the rating is a rating. **QT-1's "one live
        ticket per player" is not checked here**, deliberately: it spans
        every other row for that player, so a check-then-act would pass for
        two concurrent joins and the partial unique index is what actually
        holds (BE-06). `QueueService` checks first anyway, to produce a
        good error without a round trip.

        `ttl` is seconds and `at` is injected (AD-07), so the whole
        expiry rule is testable without a real ten minutes elapsing —
        which is the reason the clock is a port at all.
        """
        return cls(
            player_id=player_id,
            pool=pool,
            rating_snapshot=rating_snapshot,
            entered_at=at,
            expires_at=at + timedelta(seconds=ttl),
        )

    @property
    def is_waiting(self) -> bool:
        return self.status is QueueStatus.WAITING

    def is_due(self, at: datetime) -> bool:
        """Whether this ticket's window has closed by `at`.

        A *question*, not a transition: a due ticket is still `waiting`
        until something records otherwise, and both readers of this
        distinction depend on it. `QueueService.active_ticket` treats a due
        ticket as absent so a player is never blocked from re-queueing by a
        sweeper that is behind; the sweeper uses it to decide what to
        expire.

        Non-strict on the boundary (`>=`) so a ticket is due at exactly its
        expiry instant rather than one clock tick after it — the two
        readers must agree, and "not yet, come back in a microsecond" is
        not a distinction either of them can act on.
        """
        return at >= self.expires_at

    @property
    def is_reserved(self) -> bool:
        return self.status is QueueStatus.RESERVED

    def reservation_lapsed(self, at: datetime) -> bool:
        """Whether this reservation has stood past its deadline by `at`.

        A *question*, not a transition — the same shape as `is_due`, and
        for the same reason: a lapsed reservation is still `reserved` until
        the reconciler records otherwise, and what it records depends on
        whether a match was created.

        `False` for a ticket that is not reserved, so a caller filtering a
        mixed batch does not have to check the status first. Non-strict on
        the boundary (`>=`), like `is_due`, so a reservation is lapsed at
        exactly its deadline rather than one clock tick after it.
        """
        return self.reserved_until is not None and at >= self.reserved_until

    def cancelled(self, at: datetime) -> "QueueTicket":
        """The ticket a player withdrew. Raises `TicketNotWaiting`."""
        return self._resolve(QueueStatus.CANCELLED, at)

    def expired(self, at: datetime) -> "QueueTicket":
        """The ticket whose window closed. Raises `TicketNotWaiting`.

        Reachable from `reserved` as well as from `waiting`, which is the
        one place the expiry sweep sees the pairing states — an abandoned
        reservation is a ticket whose worker died, and leaving it live
        forever would lock its player out of the queue through QT-1. See
        `QueueRepository.claim_due`.
        """
        return self._resolve(QueueStatus.EXPIRED, at)

    def reserved(self, *, until: datetime) -> "QueueTicket":
        """This ticket, claimed by a pairing worker — A64-015.3.

        `waiting -> reserved`. It takes **no resolution instant**: nothing
        has resolved, the ticket is still live, and `resolved_at` stays
        `None` so the CHECK that pairs the two holds.

        What it does take is `until` — the deadline A64-015.3 predicted and
        A64-015.4 supplies. It is the same instant the match created from
        this pairing carries as its `acceptance_deadline`, which is what
        makes the reservation and the acceptance one window rather than
        two.

        Raises `TicketNotWaiting` when the ticket is anything else, which
        is the aggregate's half of "no ticket can be paired twice". The
        half that actually holds under two workers is the row lock in
        `claim_pair`; this one turns a logic error into a failure rather
        than a second reservation.
        """
        if not self.is_waiting:
            raise TicketNotWaiting("That queue ticket is no longer waiting.")
        if until <= self.entered_at:
            raise ValueError("a reservation cannot expire before its ticket was entered")
        return self._with(status=QueueStatus.RESERVED, resolved_at=None, reserved_until=until)

    def released(self) -> "QueueTicket":
        """This ticket, returned to the queue — A64-015.3's compensation.

        `reserved -> waiting`, and **`entered_at` is untouched**, which is
        the whole point: a player whose match creation failed goes back to
        the place in line they held, not to the end of it for a failure
        that was the platform's. `expires_at` is untouched for the same
        reason — the ticket's window is the one the player agreed to, and a
        failed pairing attempt is not a reason to extend or shorten it.

        Raises `TicketNotWaiting` unless the ticket is reserved: releasing
        something nobody reserved would be a compensation for an action
        that did not happen.

        `reserved_until` is cleared, because a waiting ticket has no
        reservation to be lapsed — and a stale deadline left behind would
        make the next scan's claim look already-overdue to the reconciler.
        """
        if not self.is_reserved:
            raise TicketNotWaiting("That queue ticket is not reserved.")
        return self._with(status=QueueStatus.WAITING, resolved_at=None, reserved_until=None)

    def matched(self, at: datetime) -> "QueueTicket":
        """The ticket a pairing consumed — the end of its life.

        `reserved -> matched`, never `waiting -> matched`: A64-015.3 §8
        forbids marking a ticket matched before `game` has accepted the
        match request, and requiring the reservation is how that is
        enforced rather than remembered.

        `at` is the instant the **match was created**, not the instant the
        ticket was claimed. The two differ by however long `game` took, and
        the first is the one that answers "when did this player's game
        start".
        """
        if not self.is_reserved:
            raise TicketNotWaiting("That queue ticket has not been reserved for a match.")
        return self._with(status=QueueStatus.MATCHED, resolved_at=at, reserved_until=None)

    def _resolve(self, status: QueueStatus, at: datetime) -> "QueueTicket":
        """The terminal transition, two names.

        `cancelled` and `expired` are reached the same way — from a live
        state, with an instant — so there is one place the guard lives and
        one place `resolved_at` is set. `matched` is deliberately *not*
        routed through here: its guard is narrower (reserved only), and
        collapsing the two would let a waiting ticket become matched.
        """
        if not self.status.is_live:
            raise TicketNotWaiting(
                "That queue ticket has already been resolved.",
            )
        return self._with(status=status, resolved_at=at, reserved_until=None)

    def _with(
        self,
        *,
        status: QueueStatus,
        resolved_at: datetime | None,
        reserved_until: datetime | None,
    ) -> "QueueTicket":
        """This ticket with a different status. Every other field is carried
        across verbatim, which is what makes `entered_at` survive a release
        without anybody having to remember to preserve it.

        All three arguments are required rather than defaulted, so a fifth
        transition cannot leave `reserved_until` behind by omission — which
        would be a waiting ticket the reconciler believes is a lapsed
        reservation.
        """
        return QueueTicket(
            id=self.id,
            player_id=self.player_id,
            pool=self.pool,
            rating_snapshot=self.rating_snapshot,
            entered_at=self.entered_at,
            expires_at=self.expires_at,
            status=status,
            resolved_at=resolved_at,
            reserved_until=reserved_until,
        )


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """One pool, as it stood at an instant.

    A value object rather than a list, because the two facts a pairing pass
    needs are *the tickets it may pair* and *how many there are*, and those
    are not the same number: `tickets` is bounded by
    `MATCHMAKING_SNAPSHOT_LIMIT` and `waiting` is a count over the same
    predicate. Returning only the list would make `len(tickets)` look like
    the depth, and it would be wrong by exactly the amount that matters —
    on a busy pool.

    `taken_at` is carried because a snapshot is a *reading*: by the time a
    caller acts on it, tickets may have been cancelled and others entered.
    Nothing here is a lock, and QT-4's atomic claim exists precisely because
    a snapshot cannot be one.
    """

    pool: QueuePool
    taken_at: datetime

    waiting: int
    """Every waiting, not-yet-due ticket in this pool — not the length of
    `tickets`."""

    tickets: tuple[QueueTicket, ...]
    """The oldest `waiting` tickets, entry order, bounded."""
