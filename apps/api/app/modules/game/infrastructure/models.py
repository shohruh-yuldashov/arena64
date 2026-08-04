"""The `game` schema — `match`.

The only place in this module that knows SQLAlchemy exists. Nothing above
`infrastructure/` imports this file, and the aggregate it maps to holds no
ORM type (repositories.md §3).

## What this table is, and what it is not yet

domain-model.md §10.4 describes the platform's central aggregate: seats,
position, move log, clocks, offers, result, sequence number. This relation
carries the part A64-015.4 needs and no more — **who is playing whom, under
which rules, from which pairing, and whether they have both said yes.**

The absences are deliberate rather than pending, and each names the task
that fills it:

    position, move log      live gameplay. A64-015.4 excludes move
                            submission and transport by name, and a
                            `jsonb` position nothing reads or writes would
                            be a column whose format is decided by a task
                            that has not run.
    clocks, time control    `reference.time_control` (database.md §6.2)
                            does not exist in code. See `QueuePool` on why
                            inventing one here would put the definition of
                            "blitz" in the module least entitled to own it.
    result, termination     nothing can finish a match yet.

## `pairing_id` is the load-bearing object here

`uq_match__pairing_id` is what makes A64-015.4 §3 true rather than
intended. The alternative — read by `pairing_id`, insert if absent — is
correct until two pairing workers retry one pairing at the same instant, at
which point both read nothing and both insert, and two players who agreed
to one game have two.

A unique index is a constraint the *database* checks under concurrency, so
the loser gets an `IntegrityError` and re-reads the winner's row. That is
the same reasoning `uq_queue_ticket__one_live_per_player` records, and the
same class of corruption: A-4 makes a duplicated match permanent.

Not `NULL`able and not partial: every match on this platform comes from a
pairing today, and a direct challenge (domain-model.md §21) will carry its
own idempotency key into this column rather than leaving it empty — a
nullable unique would silently permit any number of matches with no key at
all.

## Two more unique indexes, on the two ticket columns

A queue ticket produces **at most one match**. That is already true by
construction — a ticket is `reserved` once and `matched` once — and
`uq_match__light_ticket` / `uq_match__dark_ticket` are what make it true
under a bug as well. They are also the indexes
`PairingReconciliationReader` reads, so the constraint that states the
invariant is the index that answers the question the invariant is about.

## No foreign key on `player_id` or on the ticket ids

Deliberate, and the same choice `friends`, `statistics` and `matchmaking`
all make: cross-context references are opaque `player_id` values (DM-06),
and a foreign key from `game` into `matchmaking.queue_ticket` would make
the two schemas undeployable apart — precisely the seam architecture.md
§16 exists to keep open. It would also outlive its usefulness immediately:
queue tickets are prunable history and matches are permanent, so the
constraint would forbid the retention policy `matchmaking` is going to
need.

## Its own schema

`game`, per database.md §222, which assigns one schema per bounded context.
The relation is `match` rather than `game_match`, for the two reasons
`queue_ticket` records: database.md §142 wants singular `snake_case`, and
`game.game_match` says the word twice. `MATCH` is a non-reserved keyword in
PostgreSQL and SQLAlchemy quotes identifiers where it needs to, so the
qualified name is unambiguous in every statement this module issues.

## No `fillfactor`

A match row is written once and updated at most twice — once per
acceptance — and then never again. Both indexes that could make an update
non-HOT are predicated on `status`, which is exactly the column an
acceptance writes, so reserving free space would cost storage and buy
nothing. The same argument `queue_ticket` records, and the same conclusion.

## Retention applies to the churn, never to a game — A64-015.5

A64-015.4 shipped this relation with no horizon and said why: "a match is
the permanent competitive record A-4 is about, and DM-13's
anonymise-don't-delete position exists precisely so that it survives
erasure." That is still true, and it was only ever true of matches that were
*played*.

The same docstring named what would need bounding — "the pending-acceptance
**churn**: cancelled and expired rows that were never games" — and
A64-015.5 §8 supplies the horizon. `ix_match__abandoned` is the index it
claims through, and the predicate is the safety property: an `active` match
is not in it, so no configuration can reach one.

The sweep is driven by `matchmaking`, through
`game.public.AbandonedMatchRetention`. `game` owns the rows; the horizon is
the same product judgement as the queue's own ("how long is *why did my
opponent decline* answerable?"), so the module with the opinion supplies
it — the same division that already has `matchmaking` driving
`MatchAcceptanceExpiryUseCase`.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins.uuid_pk import UUIDPrimaryKeyMixin
from app.database.types import UtcDateTime
from app.modules.engine import PlayerSide
from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import ProductVariant

#: database.md §222 — one schema per bounded context.
GAME_SCHEMA = "game"


def _enum(python_type: type, name: str) -> PgEnum:
    """A native PostgreSQL enum for one of the three closed sets below.

    DB-15: closed, stable, and on columns every read filters, so four bytes
    beats a string and a typo cannot become a value no read path knows how
    to evaluate.

    `values_callable` stores the member *values* rather than the Python
    member names, matching every other enum column on the platform — a
    detail that is invisible until somebody queries the table by hand and
    finds `PENDING_ACCEPTANCE` where the API said `pending_acceptance`.
    """
    return PgEnum(
        python_type,
        name=name,
        schema=GAME_SCHEMA,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


#: The SQL spelling of `MatchRecordStatus.is_pending`, used by three
#: predicates below.
#:
#: Derived from the enum rather than typed out, so adding a fifth status
#: cannot leave one of the three saying something different from the other
#: two — which would be a constraint and an index disagreeing about whether
#: a match is still awaiting an answer.
_PENDING_PREDICATE = f"status = '{MatchRecordStatus.PENDING_ACCEPTANCE.value}'"

#: The SQL spelling of "this match never became a game" — A64-015.5 §8.
#:
#: Derived from the enum like the predicate above, so a fifth status cannot
#: silently join the set retention deletes. That is the one change to
#: `MatchRecordStatus` that needs a decision rather than a migration.
_ABANDONED_PREDICATE = (
    f"status IN ('{MatchRecordStatus.CANCELLED.value}', '{MatchRecordStatus.EXPIRED.value}')"
)

#: The SQL spelling of "this match was played to an end" — A64-016.4 §6.
#:
#: Derived from the enum for the reason the two above are: a status added
#: later must not silently join or leave the set the move log's foreign key
#: and retention's predicate disagree about.
_COMPLETED_PREDICATE = f"status = '{MatchRecordStatus.COMPLETED.value}'"

_VARIANT_ENUM = _enum(ProductVariant, "match_variant")
_STATUS_ENUM = _enum(MatchRecordStatus, "match_status")
_SIDE_ENUM = _enum(PlayerSide, "player_side")
_OUTCOME_ENUM = _enum(MatchOutcome, "match_outcome")
_TERMINATION_ENUM = _enum(TerminationReason, "match_termination_reason")


class MatchRecordModel(UUIDPrimaryKeyMixin, Base):
    """The `game.match` row.

    Composes `UUIDPrimaryKeyMixin` (application-generated UUIDv7, DB-07)
    and deliberately **not** `TimestampMixin`: a match has a `created_at`
    and a `settled_at`, and an `updated_at` would be a column that only
    ever equals one of the two — the same reasoning `QueueTicketModel` and
    `FriendshipModel` both record.

    Nor `SoftDeleteMixin`: nothing here is deleted, and the named `status`
    is what actually applies (DB-20 forbids a generic one anyway).
    """

    __tablename__ = "match"
    __table_args__ = (
        # **A64-015.4 §3, and the reason this table can be written twice
        # safely.** See this module's docstring on why a unique index
        # rather than a check-then-insert.
        Index("uq_match__pairing_id", "pairing_id", unique=True),
        # A queue ticket produces at most one match. Two indexes rather
        # than one because a ticket may be either side, and a composite
        # would not constrain the pair.
        Index("uq_match__light_ticket", "light_ticket_id", unique=True),
        Index("uq_match__dark_ticket", "dark_ticket_id", unique=True),
        # "Which match must this player answer" — one per side, both
        # partial on `pending_acceptance` so their size is bounded by
        # *concurrency* rather than by history. A player has at most one
        # pending match, so each is a single-row lookup however many games
        # they have played.
        Index(
            "ix_match__pending_light",
            "light_player_id",
            postgresql_where=text(_PENDING_PREDICATE),
        ),
        Index(
            "ix_match__pending_dark",
            "dark_player_id",
            postgresql_where=text(_PENDING_PREDICATE),
        ),
        # The acceptance-expiry sweep's claim: "pending matches whose
        # window has closed, oldest deadline first". Partial for the same
        # reason `ix_queue_ticket__due` is — a settled match can never
        # become overdue, so carrying it here would answer a question about
        # the few matches currently being answered by scanning a year.
        Index(
            "ix_match__pending_deadline",
            "acceptance_deadline",
            postgresql_where=text(_PENDING_PREDICATE),
        ),
        # QT-3's rematch guard reads "this player's most recent match",
        # which is a `DISTINCT ON (player_id) ... ORDER BY created_at DESC`
        # over both columns — so each side needs its own index leading with
        # the player and carrying the instant.
        Index("ix_match__light_player_recent", "light_player_id", "created_at"),
        Index("ix_match__dark_player_recent", "dark_player_id", "created_at"),
        # Retention's claim — A64-015.5 §8: "matches that never became
        # games, settled before X, oldest first".
        #
        # Partial on the two statuses a match reaches **without being
        # played**, which is the property that makes the job safe: an
        # `active` match is not in this index, so a retention sweep cannot
        # reach one however its horizon is configured. A `pending_acceptance`
        # one is excluded too — a pairing still awaiting an answer is not
        # abandoned however old it looks, and one that is old *and* pending
        # is a reconciliation failure the sweep must surface rather than
        # delete.
        Index(
            "ix_match__abandoned",
            "settled_at",
            postgresql_where=text(_ABANDONED_PREDICATE),
        ),
        # `settled_at` is set exactly when the handshake is over — the same
        # shape as `ck_queue_ticket__resolved_iff_terminal`, and enforced
        # here as well as in `MatchRecord.__post_init__` so a row written
        # by a repair script cannot claim an outcome without its instant
        # (BE-06).
        CheckConstraint(
            f"({_PENDING_PREDICATE}) = (settled_at IS NULL)",
            name="ck_match__settled_iff_answered",
        ),
        # A declining side is recorded exactly when the match was
        # cancelled. Without this a row could say "expired" and name
        # somebody who declined, which is the difference between an absence
        # and a decision — the whole reason the two statuses are separate.
        CheckConstraint(
            f"(declined_by IS NOT NULL) = (status = '{MatchRecordStatus.CANCELLED.value}')",
            name="ck_match__declined_iff_cancelled",
        ),
        # An active match has been accepted by both players. This is the
        # invariant §4 is about — "a newly paired Match must not become
        # ACTIVE until required acceptance succeeds" — and it is the one
        # worth having the database hold, because every other guard against
        # it lives in application code that a repair script bypasses.
        CheckConstraint(
            f"status <> '{MatchRecordStatus.ACTIVE.value}' OR (light_accepted_at IS NOT NULL "
            "AND dark_accepted_at IS NOT NULL)",
            name="ck_match__active_iff_both_accepted",
        ),
        # A window that closes before it opens is not a short window, it is
        # a match the reconciler expires on its first pass. The aggregate
        # refuses to construct one; this is the copy that also binds
        # anything writing SQL directly.
        CheckConstraint(
            "acceptance_deadline > created_at", name="ck_match__acceptance_window_positive"
        ),
        CheckConstraint("engine_version >= 1", name="ck_match__engine_version_positive"),
        # A64-016.4 §6. The three settlement invariants, each the database's
        # copy of one `MatchRecord.__post_init__` check — BE-06's rule that
        # a row written by a repair script is bound by the same rules as one
        # written by the aggregate.
        CheckConstraint(
            f"(status = '{MatchRecordStatus.COMPLETED.value}') = (outcome IS NOT NULL)",
            name="ck_match__outcome_iff_completed",
        ),
        CheckConstraint(
            "(outcome IS NOT NULL) = (ended_at IS NOT NULL)",
            name="ck_match__ended_at_iff_outcome",
        ),
        # A winner exactly for a decisive result. Without it a draw could
        # name a winner, which is the one corruption that would survive
        # every read path and reach a rating.
        CheckConstraint(
            f"(winner IS NOT NULL) = (outcome = '{MatchOutcome.WIN.value}')",
            name="ck_match__winner_iff_decisive",
        ),
        CheckConstraint("ply_number >= 0", name="ck_match__ply_non_negative"),
        {"schema": GAME_SCHEMA},
    )

    pairing_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    """The idempotency key — see this module's docstring."""

    variant: Mapped[ProductVariant] = mapped_column(_VARIANT_ENUM, nullable=False)
    """A native enum over `game.public.ProductVariant`, so the column can
    only hold something a player may actually choose: `english_8x8` is a
    `BoardVariant` and deliberately not a `ProductVariant`, and the database
    is therefore incapable of storing a match for it."""

    rated: Mapped[bool] = mapped_column(Boolean, nullable=False)

    engine_version: Mapped[int] = mapped_column(Integer, nullable=False)
    """AD-15's stamp, as the primitive `EngineVersion.as_primitive` returns.

    An `int` rather than a text version, so "played under a version older
    than the fix" is an indexable comparison rather than a parse — which is
    the query AD-15 exists to make answerable.
    """

    light_player_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    light_ticket_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    light_accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    dark_player_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dark_ticket_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dark_accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    """Written from the injected clock (AD-07), never `server_default=now()`:
    it is the ordering key of the recent-opponent read and the instant a
    settled queue ticket records, and a value assigned by the database
    would order matches by when the transaction committed."""

    acceptance_deadline: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    """The same instant both source tickets carry as `reserved_until` — see
    `game.domain.match_record` on why one value in two rows rather than two
    timers."""

    status: Mapped[MatchRecordStatus] = mapped_column(
        _STATUS_ENUM,
        nullable=False,
        server_default=MatchRecordStatus.PENDING_ACCEPTANCE.value,
    )

    declined_by: Mapped[PlayerSide | None] = mapped_column(_SIDE_ENUM, nullable=True)

    settled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # --- A64-016.4: the played half of a match's life ---------------------
    ply_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """The authoritative sequence. The column two concurrent moves contend
    on, read under the row lock that serialises them — see
    `MatchRecord.ply_number`."""

    outcome: Mapped[MatchOutcome | None] = mapped_column(_OUTCOME_ENUM, nullable=True)
    termination_reason: Mapped[TerminationReason | None] = mapped_column(
        _TERMINATION_ENUM, nullable=True
    )
    winner: Mapped[PlayerSide | None] = mapped_column(_SIDE_ENUM, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """The result, decomposed. Three columns and an instant rather than one
    blob, so `MatchResult`'s own invariant — a winner exactly for a decisive
    outcome — is a `CHECK` as well as a constructor (BE-06)."""
    """When the handshake ended. Null while it has not — see the CHECK
    above, which makes the pairing unrepresentable otherwise."""


__all__ = ["GAME_SCHEMA", "MatchRecordModel"]


class MoveLogModel(UUIDPrimaryKeyMixin, Base):
    """The `game.move` row — the durable move log. A64-016.4 §1, §2.

    AD-18's second half, and the thing A64-016.3 shipped without: "Live
    position lives in Redis. **Moves are appended durably to PostgreSQL.**"
    Until this table existed, a Redis failure lost an in-flight game with
    nothing to replay from, which is the mitigation AD-19 depends on.

    ## What this is, against `database.md` §8.4

    §8.4 specifies the relation this becomes at scale: partitioned monthly
    by `match_created_at`, `path` as `smallint[]` of PDN square numbers,
    `client_move_id`, `received_at`, non-null clock columns. This is the
    subset that ships now, documented the way `game.match` §8.2a already
    documents its own — and the divergences are decisions rather than
    omissions:

        not partitioned     `game.match` is not partitioned either, and a
                            child partitioned on a parent's key that does
                            not exist would be a foreign key that cannot be
                            declared. Both move together (DB-13)
        `path` as `text[]`  there is no PDN numbering on `BoardCoordinate`;
                            the engine round-trips algebraic notation
                            (`parse`/`__str__`) and that round trip is
                            tested. Storing a numbering the engine cannot
                            produce would be the lossy translation §4
                            forbids
        `position_hash`     `text`, because `MoveRecord.resulting_position_
                            hash` *is* `Position.fingerprint`, a string.
                            Hashing it to `bytea` would add a second
                            representation of one value
        clock columns       nullable, per §1 — they are filled by A64-016.5
        no `client_move_id` the duplicate guard is `uq_move__ply`; the
                            client's retry token is `request_id`, scoped to
                            a connection at the gateway, and A64-016.3 §7
                            forbids inventing a second one
        no `received_at`    it exists for the flag race (MT-9), which is
                            clocks

    ## Append-only, and how that is actually held

    MT-5. There is no `updated_at`, no `deleted_at` and no transition on
    `MoveRecord` — the aggregate has `_append` and nothing else. The
    stronger guarantee §8.4 asks for is a runtime role without `UPDATE` or
    `DELETE` on this table, which is a grant rather than a migration and is
    recorded in the spec as the next step.

    The `ON DELETE CASCADE` is deliberate and is not a hole in that: a move
    log without its match is unreadable, and the only thing that deletes a
    match is retention — which cannot reach a `completed` one, because
    `ix_match__abandoned` excludes it.
    """

    __tablename__ = "move"
    __table_args__ = (
        # **The concurrency mechanism** — A64-016.4 §2 and §8. Two moves
        # submitted for the same expected ply produce one row and one
        # `IntegrityError`, decided by the index rather than by a check
        # either writer would pass. It is the belt to the row lock's braces:
        # the lock serialises them, and this is what holds if a future path
        # forgets to take it.
        Index("uq_move__ply", "match_id", "ply_number", unique=True),
        # "Replay this match": every move in order. Covering, so a replay
        # reads the index and never the heap for the ordering.
        Index("ix_move__replay", "match_id", "ply_number"),
        ForeignKeyConstraint(
            ["match_id"],
            [f"{GAME_SCHEMA}.match.id"],
            name="fk_move__match",
            ondelete="CASCADE",
        ),
        CheckConstraint("ply_number >= 1", name="ck_move__ply_positive"),
        # A move covers at least two squares — the same rule
        # `Move.__post_init__` enforces, held here too because a row written
        # by anything else must not be a move the engine cannot replay.
        CheckConstraint("array_length(path, 1) >= 2", name="ck_move__path_is_a_move"),
        # §2: "position hash required for every accepted move". Non-null is
        # not enough — an empty string would satisfy it and would be a
        # replay that cannot check itself.
        CheckConstraint("position_hash <> ''", name="ck_move__position_hash_present"),
        CheckConstraint(
            "think_time_ms IS NULL OR think_time_ms >= 0", name="ck_move__think_time_non_negative"
        ),
        CheckConstraint(
            "remaining_clock_ms IS NULL OR remaining_clock_ms >= 0",
            name="ck_move__remaining_clock_non_negative",
        ),
        {"schema": GAME_SCHEMA},
    )

    match_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    ply_number: Mapped[int] = mapped_column(Integer, nullable=False)
    """1-based and contiguous (MT-5). Assigned by the transaction that
    appends the row, from the match's locked `ply_number`."""

    seat: Mapped[PlayerSide] = mapped_column(_SIDE_ENUM, nullable=False)
    """Who moved.

    Stored although it is derivable from ply parity, for §8.4's own reason:
    a takeback or an adjudicated correction breaks the parity assumption,
    and every consumer that derived seat from parity would silently
    mis-attribute every move after the first one.
    """

    path: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    """The squares the piece occupied, in order, in algebraic notation.

    **The move** (R-15). Never an origin and a destination: a multi-jump
    reaches its destination by a specific route capturing specific pieces,
    and two routes can share endpoints — so origin-and-destination produces
    an archive that cannot replay its own games.
    """

    captured: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    """The squares of the pieces taken, in the order they were jumped.

    Empty rather than null for a quiet move: an array that is sometimes
    null and sometimes empty is two representations of "nothing", and the
    reader that forgets one is the bug.
    """

    promoted_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The rank the piece ends with when the move crowns it, else `None`.

    A rank rather than a boolean, unlike §8.4's `did_promote`: a variant
    whose promotion produces something other than a king would make a
    boolean lossy, and the engine already carries `PieceRank`.
    """

    position_hash: Mapped[str] = mapped_column(Text, nullable=False)
    """`Position.fingerprint` of the position this move produced.

    Recorded per ply so a replay checks itself **as it goes** — a
    divergence caught on the move that caused it names the rule that
    changed; one caught at the end names nothing.
    """

    engine_version: Mapped[int] = mapped_column(Integer, nullable=False)
    """Stamped per move, not only per match (§8.4).

    The match's version is what it was created under; this is what actually
    applied the move. They are equal today and would not be across a
    version bump mid-match, which is precisely when a replay needs to know.
    """

    think_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remaining_clock_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Nullable until A64-016.5. AD-05: capturable only at move time, which
    is why the columns exist now — a clock task that had to backfill them
    would have nothing to backfill from."""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
