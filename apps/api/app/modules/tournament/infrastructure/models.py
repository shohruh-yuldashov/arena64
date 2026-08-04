"""The `tournaments` schema — `database.md` §3.1 reserved it, this fills it.

    tournaments.tournament       one row per tournament
    tournaments.registration     one row per (tournament, player), ever
    tournaments.round            one row per round
    tournaments.pairing          one row per bracket node
    tournaments.pairing_attempt  one row per `game` match played for a node

## No foreign key leaves this schema

`registration` references `tournament` because both are this module's. It
references **nothing** in `game` or `users` (DB-03, R-3): a player id is an
opaque cross-context identifier (DM-06), and a constraint into another
schema would make the two undeployable apart — the seam
`architecture.md` §16 exists to keep open.

## The unique key is `(tournament_id, player_id)`, without the status

Two columns, not three, and that is the decision: a withdrawn player cannot
re-enter, because the key does not admit a second row whatever the first
one's status is. §4 permits no re-registration, and this makes it
structural rather than a rule a use case could forget.

A three-column key including `status` would permit exactly the thing the
spec does not.

## Capacity is not a constraint here

There is no `CHECK` counting registrations, because a row-level constraint
cannot see the other rows. Capacity is enforced by
`SELECT ... FOR UPDATE` on the tournament row plus a count inside the same
transaction — see `application/ports.py`. That is stated here so a reader
looking for the missing constraint finds the reason rather than the gap.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins import TimestampMixin
from app.database.types import UtcDateTime
from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.domain.attempts import (
    FIRST_ATTEMPT,
    MAX_ATTEMPTS,
    AdvancementReason,
    AttemptOutcome,
    AttemptStatus,
)
from app.modules.tournament.domain.registration import RegistrationStatus
from app.modules.tournament.domain.rounds import RoundStatus
from app.modules.tournament.domain.tournament import (
    MAX_CAPACITY,
    MIN_CAPACITY,
    TournamentFormat,
    TournamentStatus,
)

#: database.md §3.1 — reserved for this feature, created here.
TOURNAMENT_SCHEMA = "tournaments"


def _enum(python_type: type, name: str) -> PgEnum:
    """A native enum in this schema, spelled as every other one is.

    Each schema owns its types — the pattern `matchmaking.queue_variant`
    and `rating.rating_variant` already set. Sharing one across schemas
    would point a `tournaments` column at a type another context can drop.
    """
    return PgEnum(
        python_type,
        name=name,
        schema=TOURNAMENT_SCHEMA,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


class TournamentModel(Base, TimestampMixin):
    """`tournaments.tournament`."""

    __tablename__ = "tournament"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    format: Mapped[TournamentFormat] = mapped_column(
        _enum(TournamentFormat, "tournament_format"), nullable=False
    )
    variant: Mapped[ProductVariant] = mapped_column(
        _enum(ProductVariant, "tournament_variant"), nullable=False
    )
    speed_class: Mapped[SpeedClass] = mapped_column(
        _enum(SpeedClass, "tournament_speed_class"), nullable=False
    )
    status: Mapped[TournamentStatus] = mapped_column(
        _enum(TournamentStatus, "tournament_status"), nullable=False
    )

    rated: Mapped[bool] = mapped_column(nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)

    created_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    """The administrator, or `NULL` for a system tournament — T-3.

    No foreign key into `users`: a player id is an opaque cross-context
    identifier (DM-06), and the constraint would couple two schemas that
    must deploy apart."""

    registration_deadline: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """When registration closes on its own, or `NULL` for operator-closed.

    The column the deadline sweep claims on — see
    `infrastructure/tasks.py`."""

    __table_args__ = (
        # The sweep's whole query: open tournaments whose deadline has
        # passed. Partial, because a tournament without a deadline is never
        # claimed and a closed one never again.
        Index(
            "ix_tournament__overdue",
            "registration_deadline",
            postgresql_where=text(
                f"status = '{TournamentStatus.REGISTRATION_OPEN.value}' "
                "AND registration_deadline IS NOT NULL"
            ),
        ),
        CheckConstraint(
            f"capacity BETWEEN {MIN_CAPACITY} AND {MAX_CAPACITY}",
            name="ck_tournament__capacity_in_range",
        ),
        {"schema": TOURNAMENT_SCHEMA},
    )


class RegistrationModel(Base, TimestampMixin):
    """`tournaments.registration` — one row per player per tournament, ever."""

    __tablename__ = "registration"

    tournament_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    player_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)

    status: Mapped[RegistrationStatus] = mapped_column(
        _enum(RegistrationStatus, "registration_status"), nullable=False
    )
    registered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    withdrawn_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    seed_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """This entrant's seed, assigned when the tournament was seeded — §4.

    **Persisted rather than recomputed.** Ratings move; the bracket does
    not. A later phase that re-derived seeding from current ratings would
    produce a different order from the one that was published, which is the
    same class of error as reseeding a tournament mid-round.

    `NULL` until seeding runs, which is also what makes seeding idempotent:
    a second attempt finds the numbers already there.
    """

    __table_args__ = (
        # "How many slots are taken" — the count the capacity guard runs
        # inside its lock. Partial on the status, so it is an index over
        # exactly the rows that count.
        Index(
            "ix_registration__active",
            "tournament_id",
            postgresql_where=text(f"status = '{RegistrationStatus.REGISTERED.value}'"),
        ),
        ForeignKeyConstraint(
            ["tournament_id"],
            [f"{TOURNAMENT_SCHEMA}.tournament.id"],
            name="fk_registration__tournament",
            # Within one schema, so the coupling is this module's own. No
            # cascade: a tournament's entrants are part of its permanent
            # record, and deleting a tournament that has them is a decision
            # rather than a side effect.
            ondelete="RESTRICT",
        ),
        # The two instants travel together: a withdrawn entry with no
        # instant is unanswerable when somebody asks when it happened.
        CheckConstraint(
            f"(status = '{RegistrationStatus.WITHDRAWN.value}') = (withdrawn_at IS NOT NULL)",
            name="ck_registration__withdrawn_iff_instant",
        ),
        {"schema": TOURNAMENT_SCHEMA},
    )


class TournamentRoundModel(Base, TimestampMixin):
    """`tournaments.round` — one round's lifecycle. §2.

    The status machine lives in `domain/rounds.py`; this stores where a
    round is, never how it may move. A repository that decided transitions
    would be a second copy of the rule, and the copy is what goes stale.
    """

    __tablename__ = "round"

    tournament_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    round_number: Mapped[int] = mapped_column(Integer, primary_key=True)

    status: Mapped[RoundStatus] = mapped_column(_enum(RoundStatus, "round_status"), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tournament_id"],
            [f"{TOURNAMENT_SCHEMA}.tournament.id"],
            name="fk_round__tournament",
            ondelete="RESTRICT",
        ),
        CheckConstraint("round_number >= 1", name="ck_round__number_from_one"),
        # Each instant is set by the transition that names it, so a round
        # that says it is published without saying when is a write that
        # skipped the aggregate.
        CheckConstraint(
            "(status = 'pending') = (published_at IS NULL)",
            name="ck_round__published_iff_instant",
        ),
        {"schema": TOURNAMENT_SCHEMA},
    )


class PairingModel(Base, TimestampMixin):
    """`tournaments.pairing` — one slot of one round.

    Written when a round's plan is created and **never rewritten**: §6's
    immutability is the primary key doing the work, since a second plan for
    the same slot cannot be inserted. That is also what makes seeding
    idempotent — a retry collides rather than producing a second bracket.

    Both player columns are nullable because a **bye is an empty slot**
    (§7), not a fake player. Exactly one filled means a bye; both filled
    means a match will be created in A64-019.5; neither cannot happen in
    round one and is left unconstrained because later rounds legitimately
    start empty.
    """

    __tablename__ = "pairing"

    tournament_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    round_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4
    )
    """A stable surrogate identity, **beside** the coordinates — §6c.

    The primary key stays `(tournament_id, round_number, slot)`, because
    that is what makes a second plan for one slot impossible and is the
    whole of §6's immutability. What the coordinates cannot be is a
    reference handed to another context: a match records
    `origin_ref = pairing.id` (R-25), and encoding a tournament, a round and
    a slot into that would publish this module's own arithmetic and freeze
    it — a bracket that later renumbered rounds would orphan every match
    already played.

    `default` here and `server_default` in the migration, deliberately both:
    the Python default is what lets the ORM know the id it inserted without
    a round trip, and the server default is what gives a row written by a
    repair script or a backfill an id at all.
    """

    light_player_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    dark_player_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    light_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dark_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)

    winner_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    """Who advanced from this node — §7.

    The **compare-and-set target**: advancement is
    `UPDATE … SET winner_id = :w WHERE … AND winner_id IS NULL`, so two
    workers processing the same completed match cannot both write. Read
    then write would let both through.
    """

    advancement_reason: Mapped[AdvancementReason | None] = mapped_column(
        _enum(AdvancementReason, "advancement_reason"), nullable=True
    )
    """**Why** this node's winner advanced, or `NULL` while it has none.

    Beside `winner_id` rather than derived from it, because the three cases
    are indistinguishable afterwards and one of them is a competitive fact
    somebody will ask about: a player who won a game, a player who had no
    opponent, and a player the bracket awarded the node to after two draws
    (§6c). Recomputing "was this an adjudication" from the attempt rows
    would work until the attempts are pruned and the tournament is not.
    """

    __table_args__ = (
        ForeignKeyConstraint(
            ["tournament_id"],
            [f"{TOURNAMENT_SCHEMA}.tournament.id"],
            name="fk_pairing__tournament",
            ondelete="RESTRICT",
        ),
        CheckConstraint("round_number >= 1", name="ck_pairing__round_from_one"),
        CheckConstraint("slot >= 0", name="ck_pairing__slot_not_negative"),
        # A seat with a player has a seed and vice versa. Not decoration:
        # the seed is how a later phase explains *why* this pairing exists,
        # and half of one would be a bracket that cannot be justified.
        CheckConstraint(
            "(light_player_id IS NULL) = (light_seed IS NULL)",
            name="ck_pairing__light_seat_is_complete",
        ),
        CheckConstraint(
            "(dark_player_id IS NULL) = (dark_seed IS NULL)",
            name="ck_pairing__dark_seat_is_complete",
        ),
        # Nobody plays themselves — the one malformed pairing this relation
        # can detect on its own.
        CheckConstraint(
            "light_player_id IS NULL OR dark_player_id IS NULL "
            "OR light_player_id <> dark_player_id",
            name="ck_pairing__distinct_players",
        ),
        # A winner played here. The database's half of the rule
        # `BracketSlot.with_winner` enforces — a row written by a backfill
        # cannot advance somebody who was never in the node.
        CheckConstraint(
            "winner_id IS NULL OR winner_id = light_player_id OR winner_id = dark_player_id",
            name="ck_pairing__winner_played_here",
        ),
        # A reason accompanies a winner and never stands alone — the same
        # "both halves of a fact or neither" the withdrawal instant keeps.
        # A node with a reason and no winner would claim an advancement
        # nobody made.
        CheckConstraint(
            "(winner_id IS NULL) = (advancement_reason IS NULL)",
            name="ck_pairing__reason_iff_winner",
        ),
        {"schema": TOURNAMENT_SCHEMA},
    )


class PairingAttemptModel(Base, TimestampMixin):
    """`tournaments.pairing_attempt` — one `game` match played for one node.

    A **relation rather than a list in a column**, and §6c's reason is that
    the rules a list would need enforcing in code are constraints here: one
    row per attempt, one match per attempt, and no third attempt. A pairing
    with two matches modelled as `match_id` and `rematch_match_id` would be
    two columns and a third the day the bound changes.

    `pairing.match_id` is **removed** in the same migration. It could no
    longer truthfully represent a pairing with two matches, and two
    competing sources of truth is worse than a migration — the feature is
    unreleased, so the model is corrected now rather than preserved.

    ## The foreign key is to `pairing.id`, not to its coordinates

    Three columns would work and would tie every attempt to a round number
    and a slot — the arithmetic §6c deliberately keeps free to change. The
    surrogate is what makes the reference opaque on both sides of the
    boundary: `game` holds it as `origin_ref`, and so does this.
    """

    __tablename__ = "pairing_attempt"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)

    pairing_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    match_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    """The `game` match this attempt was played as.

    Opaque and **not null**: an attempt exists because a match was created
    for it, so a row without one would be an attempt that never happened.
    No foreign key in either direction (DB-03) — a tournament is prunable
    and a match is permanent, so the constraint would forbid a retention
    the other schema will need.
    """

    light_player_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    dark_player_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    """The seats **as this attempt was played**, not as the pairing plans
    them: a rematch swaps them (§6c), so reading the pairing to learn who
    was light in attempt two would give the wrong answer."""

    status: Mapped[AttemptStatus] = mapped_column(
        _enum(AttemptStatus, "attempt_status"), nullable=False
    )
    outcome: Mapped[AttemptOutcome | None] = mapped_column(
        _enum(AttemptOutcome, "attempt_outcome"), nullable=True
    )
    winner_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    no_show_deadline: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """When this attempt stops waiting for absent players — §6e.

    Written at creation from `TOURNAMENT_NO_SHOW_SECONDS` and never
    recomputed, so a deploy that changes the setting cannot move a deadline
    a player was already given. `NULL` only for a row written before
    A64-019.6, which the sweep's predicate therefore never claims.
    """

    light_present_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    dark_present_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """When each player **first** reached the match.

    Set once by a guarded `UPDATE ... WHERE ... IS NULL` and never cleared,
    because §6e's rule is that a transient disconnect after somebody turned
    up is not a no-show. A "connected now" flag would make a dropped socket
    indistinguishable from an absence.
    """

    __table_args__ = (
        ForeignKeyConstraint(
            ["pairing_id"],
            [f"{TOURNAMENT_SCHEMA}.pairing.id"],
            name="fk_pairing_attempt__pairing",
            ondelete="RESTRICT",
        ),
        # The no-show sweep's whole query: unsettled attempts whose deadline
        # has passed. Partial on `outcome IS NULL`, so it indexes exactly the
        # rows that can still be claimed and shrinks as a tournament is
        # played rather than growing with it.
        Index(
            "ix_pairing_attempt__no_show_due",
            "no_show_deadline",
            postgresql_where=text("outcome IS NULL AND no_show_deadline IS NOT NULL"),
        ),
        # **The idempotency guarantee.** A redelivered `match.completed`
        # cannot create a second rematch, because the row it would insert
        # already exists — §6c. Read-then-insert would let two deliveries
        # through, and two rematches for one pairing is two games two
        # players did not agree to.
        Index(
            "uq_pairing_attempt__pairing_number",
            "pairing_id",
            "attempt_number",
            unique=True,
        ),
        # One attempt per match. Two attempts claiming one match would be
        # two advancements from one result — the guarantee `uq_pairing__match`
        # used to hold, moved to where the match now lives.
        Index("uq_pairing_attempt__match", "match_id", unique=True),
        # The bound, in the schema. A third attempt cannot exist even if
        # something above forgets §6c's rule — which matters because an
        # unbounded rematch chain is a tournament that never finishes.
        CheckConstraint(
            f"attempt_number BETWEEN {FIRST_ATTEMPT} AND {MAX_ATTEMPTS}",
            name="ck_pairing_attempt__number_in_range",
        ),
        # A completed attempt has an outcome and an instant; a live one has
        # neither. Three columns that must agree, so the database holds the
        # agreement rather than every writer remembering it.
        CheckConstraint(
            f"(status = '{AttemptStatus.COMPLETED.value}') = (outcome IS NOT NULL) "
            f"AND (status = '{AttemptStatus.COMPLETED.value}') = (completed_at IS NOT NULL)",
            name="ck_pairing_attempt__completed_iff_outcome",
        ),
        # A winner played in this attempt, and a draw has none. The same
        # rule `ck_pairing__winner_played_here` keeps one level up, and the
        # one error nothing downstream detects.
        CheckConstraint(
            "winner_id IS NULL OR winner_id = light_player_id OR winner_id = dark_player_id",
            name="ck_pairing_attempt__winner_played_here",
        ),
        # A draw names nobody; every other settled outcome names somebody.
        # Written this way round rather than as "decisive implies a winner"
        # because A64-019.6 added `no_show`, which also advances a player —
        # and a rule phrased in terms of the members that *do* is one a
        # third member silently escapes.
        CheckConstraint(
            f"outcome IS NULL OR (outcome = '{AttemptOutcome.DRAW.value}') = (winner_id IS NULL)",
            name="ck_pairing_attempt__winner_iff_decisive",
        ),
        CheckConstraint(
            "light_player_id <> dark_player_id",
            name="ck_pairing_attempt__distinct_players",
        ),
        {"schema": TOURNAMENT_SCHEMA},
    )


__all__ = [
    "TOURNAMENT_SCHEMA",
    "TournamentRoundModel",
    "PairingAttemptModel",
    "PairingModel",
    "RegistrationModel",
    "TournamentModel",
]
