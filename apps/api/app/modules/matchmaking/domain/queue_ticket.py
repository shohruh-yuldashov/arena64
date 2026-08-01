"""`QueueTicket` — one player's standing request to be paired.

domain-model.md §10.2, and the aggregate root architecture.md §6 assigns to
`matchmaking`. Framework-free (architecture.md §8): no SQL, no Redis, no
clock — time arrives as an argument (AD-07).

## The four states, and the three this task does not implement

A64-014.1 specifies `waiting`, `matched`, `cancelled`, `expired`.
domain-model.md §10.2's diagram has seven, and the difference is worth
recording rather than resolving silently, because the missing three are
*future* states and not omissions:

    Queued      -> `waiting`
    Widening    -> not modelled. QT-5's widening rating window is a
                   property of a *pairing scan*, not of a ticket: the
                   ticket carries `entered_at` and the scan derives the
                   window from its age. A state whose only content is "the
                   scan has looked at this a few times" is state the scan
                   can recompute, and one more transition to get wrong.
    Reserved    -> A64-014.2. The two-phase claim QT-4 describes — a
                   worker takes both tickets, then creates the match — is
                   what `reserved` is for, and there is no pairing here to
                   reserve anything.
    Consumed    -> `matched`. The name follows A64-014.1 rather than
                   domain-model.md; both mean "this ticket produced a
                   match and is finished".
    Cancelled   -> `cancelled`
    Expired     -> `expired`
    Abandoned   -> not modelled. It is `expired` with a different cause,
                   and the cause is only knowable once presence is watched
                   continuously rather than checked at entry. A64-014.2.

**Preparing for acceptance** (the task's own words) is what `matched` and
`resolved_at` are for: an acceptance flow inserts `reserved` before
`matched` and adds a deadline, and neither changes anything already
written — the terminal states stay terminal and the partial unique index
that enforces QT-1 keys on `waiting` alone, which `reserved` would join.

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

    Exactly one of these is non-terminal, and every rule in this module is
    a statement about that asymmetry: `waiting` is the only state a ticket
    can leave, the only one QT-1's uniqueness covers, and the only one a
    pairing scan will ever read.
    """

    WAITING = "waiting"
    MATCHED = "matched"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self is not QueueStatus.WAITING


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
            raise ValueError("resolved_at is set exactly when the ticket has left `waiting`")

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

    def cancelled(self, at: datetime) -> "QueueTicket":
        """The ticket a player withdrew. Raises `TicketNotWaiting`."""
        return self._resolve(QueueStatus.CANCELLED, at)

    def expired(self, at: datetime) -> "QueueTicket":
        """The ticket whose window closed. Raises `TicketNotWaiting`."""
        return self._resolve(QueueStatus.EXPIRED, at)

    def matched(self, at: datetime) -> "QueueTicket":
        """The ticket a pairing consumed. Raises `TicketNotWaiting`.

        **Nothing calls this yet**, and it is here rather than in A64-014.2
        because `matched` is one of the four states this task specifies and
        a status a transition cannot reach is a status the database can
        hold and the domain cannot explain. The pairing worker that calls
        it will pass the instant the match was created, not the instant it
        claimed the ticket — see QT-4 on why the claim and the creation are
        separable and why the compensating path returns the ticket to
        `waiting` rather than through here.
        """
        return self._resolve(QueueStatus.MATCHED, at)

    def _resolve(self, status: QueueStatus, at: datetime) -> "QueueTicket":
        """The one transition, three names.

        Every terminal state is reached the same way — from `waiting`, with
        an instant — so there is one place the guard lives and one place
        `resolved_at` is set. Three separate bodies would be three chances
        to forget the second.
        """
        if not self.is_waiting:
            raise TicketNotWaiting(
                "That queue ticket has already been resolved.",
            )
        return QueueTicket(
            id=self.id,
            player_id=self.player_id,
            pool=self.pool,
            rating_snapshot=self.rating_snapshot,
            entered_at=self.entered_at,
            expires_at=self.expires_at,
            status=status,
            resolved_at=at,
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
