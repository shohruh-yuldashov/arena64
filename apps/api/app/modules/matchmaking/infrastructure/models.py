"""The `matchmaking` schema — `queue_ticket`.

The only place in this module that knows SQLAlchemy exists. Nothing above
`infrastructure/` imports this file, and the aggregate it maps to holds no
ORM type (repositories.md §3).

## This table contradicts database.md §8.1, and the divergence is the point

§8.1 says, in its own words: "**Queue tickets are absent from PostgreSQL
entirely.** They are Redis-authoritative (AD-18) and their lifetime is
seconds. A durable ticket would need sweeping, would survive a closed tab,
and would put a PostgreSQL write on the queue-entry path for state that is
meaningless the moment the player disconnects." domain-model.md's entity
table says the same (row 17, store: **Redis**).

A64-014.1 requires a durable table, and CLAUDE.md is explicit about what to
do with that: the user wins, the divergence is stated, and the documents
change in the same pull request (§3.11, §4.1). They have — see database.md
§8.1 and domain-model.md §10.2 as of this task. What follows is the argument
that was recorded there, restated here because this is the file somebody
reads when they wonder why the document they remember says otherwise.

**The three objections, answered.**

*"A durable ticket would need sweeping."* It does, and the sweep is
`QueueService.expire_due`. That is not a new cost the Redis design avoided:
a sorted set of tickets needs the identical sweep, because a `ZADD`ed member
does not expire — only whole keys do — so the ephemeral design would have
needed either a per-ticket key (losing the score-range query that was its
whole reason for existing) or exactly this sweeper against Redis instead.

*"It would survive a closed tab."* It does, for at most
`MATCHMAKING_TICKET_TTL_SECONDS`. That is a feature rather than a leak: a
player whose train goes into a tunnel for forty seconds keeps their place,
which the ephemeral design could not offer, and `expires_at` bounds how long
a genuinely departed player occupies a pool.

*"It would put a PostgreSQL write on the queue-entry path."* It does — one
indexed insert, on an endpoint a human pressed. The comparison that matters
is not "write versus no write" but "write versus the alternative", and the
alternative is what decided this:

  **QT-4's atomic claim is unimplementable in Redis without inventing a
  concurrency mechanism.** "Claiming both tickets is atomic" over a sorted
  set means either a Lua script that reimplements row locking, or an
  optimistic loop, or a distributed lock — three designs A64-014.1
  explicitly forbids ("Do NOT invent another concurrent claiming
  mechanism"). `SELECT ... FOR UPDATE SKIP LOCKED` is the platform's proven
  answer to exactly this problem, it is already carrying the outbox, and it
  exists only in PostgreSQL.

  **A-4 makes double-pairing a permanent corruption.** QT-1 is enforced here
  by a partial unique index — a constraint the database checks under
  concurrency. In Redis it would be a check-then-act in application code,
  which is correct until two joins race, and the consequence of losing that
  race is a player in two simultaneous matches, one of which must be
  abandoned.

What is *not* claimed: that this is cheaper. It is one write where there
would have been none, and a pool of ten thousand waiting players is ten
thousand rows rather than one sorted set. Both are affordable at
system-design.md's target concurrency, and neither buys anything the two
paragraphs above give up.

**Redis is not gone from matchmaking**, and caching.md's `matchmaking`
allocation now says which part it keeps: a sorted set per pool scored by
rating remains the right *index* for a widening-window scan, derived from
this table and rebuildable from it (AD-19). A64-014.2 adds it if a
measurement asks for it. Nothing is Redis-authoritative here.

## Its own schema

`matchmaking`, per database.md §222, which assigns one schema per bounded
context — the same schema §8.1 already reserves for `challenge`. That is
what makes architecture.md §16's extraction seam real rather than
aspirational.

## The table is `queue_ticket`, not `matchmaking_queue`

A64-014.1 names the table `matchmaking_queue`. It is `queue_ticket` here,
and the divergence is recorded rather than resolved silently — the same
call `blocked_player` made against A64-013.5's `blocked_players`, for two
reasons that both come from database.md:

  - **Singular** (§142: "Table: `snake_case`, **singular**").
  - **No schema stutter.** The qualified name is what appears in every
    query, every migration and every `psql` session, and
    `matchmaking.matchmaking_queue` says the word twice.

`queue_ticket` is also the aggregate root's name in domain-model.md §10.2
and in A64-014.1's own Domain section, and one repository per aggregate root
(repositories.md) means the table is that aggregate's.

## No foreign key on `player_id`

Deliberate, and the same choice `friends` and `statistics` make: cross-
context references are opaque `player_id` values (DM-06), and a foreign key
from `matchmaking` into `users` would make the two schemas undeployable
apart — which is precisely the seam §16 exists to keep open.

## No `fillfactor`, unlike `platform.outbox`

Both relations have the same churn shape — written once, updated once, then
dead — so DB-18's argument appears to apply. It does not, and the reason is
worth stating so nobody adds one later on the resemblance.

`fillfactor` below 100 buys **HOT** updates, and an update is HOT only when
no indexed column changes. Every index below is predicated on `status`, and
`status` is exactly the column the one update writes. The update therefore
can never be HOT here whatever the fill factor, so reserving free space on
every page would cost storage and buy nothing.

## Known limitation: resolved tickets are retained without a horizon

A64-014.1 requires retention for `platform.outbox` and says nothing about
this relation, and none was built — so **resolved tickets accumulate**. It
is recorded here rather than left to be discovered, because CLAUDE.md §10.5
asks for everything unbounded to be bounded and this is not.

The consequence is bounded in the ways that matter and unbounded in the way
that eventually does not:

  - **No read degrades.** Every index above is partial on `waiting`, so
    every query this module issues touches the live queue only. A million
    resolved rows make no scan slower.
  - **Storage grows with matches attempted, forever.** At
    system-design.md's target that is on the order of a million rows a
    month, which is small for a year and is not a policy.

The fix is `platform.outbox.retention`'s, applied to this table — the same
`RetentionPolicy`, the same bounded `SKIP LOCKED` batches, a second
registered task. It is A64-014.2's, and it is listed in this task's
recommendations rather than built here because a retention horizon for
queue history is a product decision (how long is "why was I matched with
them" answerable?) that pairing has not been built to ask yet.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, Integer, Uuid, text
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins.uuid_pk import UUIDPrimaryKeyMixin
from app.database.types import UtcDateTime
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.domain.queue_pool import QueueType, Region
from app.modules.matchmaking.domain.queue_ticket import QueueStatus

#: database.md §222 — one schema per bounded context. `challenge` (§8.1)
#: joins it when direct invitations are built.
MATCHMAKING_SCHEMA = "matchmaking"


def _enum(python_type: type, name: str) -> PgEnum:
    """A native PostgreSQL enum for one of the three closed sets below.

    DB-15: closed, stable, and on columns every pool query filters, so four
    bytes beats a string and a typo cannot become a value no read path knows
    how to evaluate.

    `values_callable` stores the member *values*, not the Python member
    names, matching every other enum column on the platform — a detail that
    is invisible until somebody queries the table by hand and finds
    `NORTH_AMERICA` where the API said `north_america`.

    A helper rather than three near-identical literals because the three
    differ only in two arguments, and the argument that must not vary — the
    `values_callable` — is the one that would be forgotten.
    """
    return PgEnum(
        python_type,
        name=name,
        schema=MATCHMAKING_SCHEMA,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


#: The SQL spelling of `QueueStatus.is_live`, used by three predicates
#: below.
#:
#: Derived from the enum rather than typed out, so adding a sixth status
#: cannot leave one of the three saying something different from the other
#: two — which would be a constraint and an index disagreeing about whether
#: a player is queued.
_LIVE_PREDICATE = "status IN ({})".format(
    ", ".join(f"'{status.value}'" for status in QueueStatus if status.is_live)
)

_VARIANT_ENUM = _enum(ProductVariant, "queue_variant")
_QUEUE_TYPE_ENUM = _enum(QueueType, "queue_type")
_REGION_ENUM = _enum(Region, "queue_region")
_STATUS_ENUM = _enum(QueueStatus, "queue_ticket_status")


class QueueTicketModel(UUIDPrimaryKeyMixin, Base):
    """The `matchmaking.queue_ticket` row.

    Composes `UUIDPrimaryKeyMixin` (application-generated UUIDv7, DB-07) and
    deliberately **not** `TimestampMixin`. A ticket has an `entered_at` and
    a `resolved_at` and there is no third thing to update: the row is
    written once and resolved once, so an `updated_at` would be a column
    that only ever equals one of the other two — the same reasoning
    `FriendshipModel` records.

    Nor `SoftDeleteMixin`: nothing here is deleted, and the named `status`
    is what actually applies (DB-20 forbids a generic one anyway).
    """

    __tablename__ = "queue_ticket"
    __table_args__ = (
        # **QT-1, and the reason this table is in PostgreSQL.** One live
        # ticket per player, across every pool — the `queue_type` and
        # `region` columns are deliberately absent from the key, because
        # "one per pool" would permit exactly the multi-queueing that pairs
        # a player into two simultaneous matches.
        #
        # **Partial**, for the reason `uq_friendship__pair` is: a plain
        # unique on `player_id` would mean a player could queue once ever.
        # The constraint is on the *live* state, which is what QT-1 says.
        #
        # A64-015.3 widened "live" from `waiting` to `waiting, reserved`.
        # A reserved player is mid-pairing, and letting them join a second
        # pool while a worker creates their match is precisely the
        # multi-queueing this index exists to prevent — the ticket has left
        # `waiting` without leaving the queue.
        #
        # This is the authoritative check (BE-06). `QueueService.join` reads
        # first to produce a good error cheaply, and two concurrent joins
        # both pass that read — only this index is correct under
        # concurrency, and losing that race is A-4-grade corruption rather
        # than a duplicate row.
        Index(
            "uq_queue_ticket__one_live_per_player",
            "player_id",
            unique=True,
            postgresql_where=text(_LIVE_PREDICATE),
        ),
        # The pairing scan's index, and the snapshot's — leading with the
        # pool because every read of this relation names one, and carrying
        # `entered_at, id` after it because every such read is ordered
        # oldest-first. That order lets PostgreSQL walk the index and stop
        # at the page size instead of sorting the pool.
        #
        # Partial on `waiting`, so its size is bounded by *concurrency*
        # rather than by history — the same property that makes
        # `ix_outbox__unpublished` a direct measure of relay health. A pool
        # index carrying every ticket ever queued would answer a question
        # about the few hundred people currently waiting by scanning a year.
        Index(
            "ix_queue_ticket__pool",
            "variant",
            "queue_type",
            "region",
            "entered_at",
            "id",
            postgresql_where=text("status = 'waiting'"),
        ),
        # The expiry sweep's claim: "live tickets whose deadline has
        # passed, oldest first". Its own index rather than a reuse of the
        # pool index above, because the sweep is deliberately **pool-blind**
        # — one worker drains every pool, and a scan that had to lead with
        # `queue_type` would need one pass per pool per tick.
        #
        # Covers `reserved` as well as `waiting` (A64-015.3), which is the
        # one place the sweep sees the pairing states. A worker that dies
        # between reserving a pair and settling it leaves two reserved
        # tickets, and a reserved ticket is *live* — so without this it
        # would occupy QT-1's index forever and lock its player out of the
        # queue permanently. Once its own window closes it is abandoned by
        # any measure, and the sweep expires it.
        Index(
            "ix_queue_ticket__due",
            "expires_at",
            postgresql_where=text(_LIVE_PREDICATE),
        ),
        # The reconciler's claim: "reservations that have stood past their
        # own deadline, oldest first" — A64-015.4 §9.
        #
        # Its own index rather than a reuse of `ix_queue_ticket__due`,
        # because it leads with a different column and answers a different
        # question. `expires_at` is when a *player* has waited long enough;
        # `reserved_until` is when a *worker* was supposed to have finished,
        # and it is two orders of magnitude shorter. A sweep that had to
        # scan the first to find the second would read every waiting ticket
        # in the queue to find the two that crashed.
        #
        # Partial on `reserved` alone, which is narrower than
        # `_LIVE_PREDICATE`: `reserved_until` is null on every other status
        # by CHECK, so the index would carry nothing but nulls for them.
        Index(
            "ix_queue_ticket__stale_reservation",
            "reserved_until",
            postgresql_where=text(f"status = '{QueueStatus.RESERVED.value}'"),
        ),
        # `resolved_at` is set exactly when the ticket is no longer live —
        # the same shape as `ck_friend_request__responded_iff_resolved`, and
        # enforced here as well as in `QueueTicket.__post_init__` so a row
        # written by a repair script cannot claim an outcome without its
        # instant (BE-06).
        #
        # `reserved` is on the live side of this: a reservation is not an
        # outcome, and stamping one would make a ticket that goes back to
        # `waiting` carry the instant of a match that never happened.
        CheckConstraint(
            f"({_LIVE_PREDICATE}) = (resolved_at IS NULL)",
            name="ck_queue_ticket__resolved_iff_terminal",
        ),
        # A reservation deadline exists exactly while there is a
        # reservation — A64-015.4 §5. Without it a released ticket could
        # keep the deadline it was reserved under, and the reconciler would
        # then see a `waiting` row it believes is a stranded reservation;
        # and a reserved row could carry none, which would make it
        # invisible to the recovery job forever.
        CheckConstraint(
            f"(status = '{QueueStatus.RESERVED.value}') = (reserved_until IS NOT NULL)",
            name="ck_queue_ticket__reserved_iff_deadline",
        ),
        # A ticket that expired before it was entered is not a short
        # window, it is a ticket the sweeper takes on its first pass. The
        # aggregate refuses to construct one; this is the copy that also
        # binds anything writing SQL directly.
        CheckConstraint("expires_at > entered_at", name="ck_queue_ticket__window_positive"),
        # No rating system produces a negative rating (see
        # `profiles.domain.ratings`), so one here means a provider is broken
        # rather than a player is bad — and a negative snapshot would sort
        # to the front of every widening scan.
        CheckConstraint("rating_snapshot >= 0", name="ck_queue_ticket__rating_non_negative"),
        {"schema": MATCHMAKING_SCHEMA},
    )

    player_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    """Opaque across contexts (DM-06) — see this module's docstring on why
    there is no foreign key."""

    variant: Mapped[ProductVariant] = mapped_column(_VARIANT_ENUM, nullable=False)
    """Which rule set a game from this ticket's pool would be played under
    — A64-015.2.

    A native enum over `game.public.ProductVariant`, so the column can only
    hold something a player may actually choose: `english_8x8` is a
    `BoardVariant` and is deliberately not a `ProductVariant`, and the
    database is therefore incapable of storing a ticket for it
    (`specs/game-engine/audit.md` §9).

    Added before any ticket existed in production, which is the only cheap
    moment. A pool is `(variant, mode, region)` — see `QueuePool` — and a
    ticket that did not record which game it was waiting for could not be
    excluded from the wrong pairing scan.
    """

    queue_type: Mapped[QueueType] = mapped_column(_QUEUE_TYPE_ENUM, nullable=False)
    region: Mapped[Region] = mapped_column(_REGION_ENUM, nullable=False)

    rating_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    """QT-2's rating **at entry**. `Integer` rather than a numeric type
    because every rating system this platform might choose reports a whole
    number publicly — Glicko-2's deviation and volatility, if Q-3 lands
    there, are pairing inputs that belong beside this column rather than
    inside it."""

    entered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    """Written from the injected clock (AD-07), never `server_default=now()`:
    it is the ordering key of every pairing scan, and a value assigned by
    the database would order tickets by when the transaction committed
    rather than by when the player pressed the button."""

    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    status: Mapped[QueueStatus] = mapped_column(
        _STATUS_ENUM, nullable=False, server_default=QueueStatus.WAITING.value
    )

    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """When the ticket left `waiting`. Null while it has not — see the
    CHECK above, which makes the pairing unrepresentable otherwise."""

    reserved_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """How long this reservation may stand before it is reconciled —
    A64-015.4 §5.

    The same instant the match created from this pairing carries as its
    `acceptance_deadline`: one number, written to two rows in two schemas,
    so the reservation window and the acceptance window cannot drift apart.
    See `QueueTicket.reserved_until`.
    """
