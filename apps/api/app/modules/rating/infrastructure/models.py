"""`rating`'s two relations — SPEC-RATING §10.

    rating.player_rating       one row per (player, variant, speed class)
    rating.rating_adjustment   one row per (player, match) — permanent

One schema per bounded context (database.md §222). Both are PostgreSQL and
**never Redis** (AD-19, caching.md C-5): a rating that exists only in Redis
is one an eviction policy can delete, with no recovery path, and A-4 says
ratings are permanent.

## The unique index is the exactly-once mechanism, not a safety net

PR-1 — *"a match affects a rating exactly once, enforced at the database,
not in code"* — is the single most important invariant on this platform, and
`uq_rating_adjustment__player_match` is where it lives.

Check-then-insert is **not** sufficient and is the reason this is stated
here rather than left to the repository: two deliveries of the same
`game.match_completed` can both find no row and both insert. The window is
small and the traffic that finds it is exactly a relay retrying, which is
the normal case rather than an unlucky one.

So the handler inserts and treats a unique violation as *success* — the
work was already done by whoever won the race.

**Two columns, not three.** SPEC-RATING §7.1 keys a rating by
`(variant, speed_class)`, so an adjustment carries one too; but a match
belongs to exactly one key, which makes `(player_id, match_id)` strictly
*stronger* than adding the key to the index. A three-column index would
permit two adjustments for one player and one match under different keys —
which is precisely the double-rating PR-1 forbids.

## Why the triple is three columns rather than a composite type

`value`, `deviation` and `volatility` are stored as three `double precision`
columns on each of five roles (the rating, its before/after on an adjustment,
and the opponent's). A composite type or a JSON blob would read more tidily
and would make every one of them unindexable and unconstrainable — and the
leaderboard orders by `value`.

## Storage of the rating *value*

`double precision`, not `numeric` and not an integer. Glicko-2's arithmetic
is floating point throughout, an integer column would round on every write
and the rounding would accumulate across a career, and `numeric` would buy
exactness the algorithm does not have anyway. What a player is *shown* is
rounded at the presentation boundary.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins import TimestampMixin
from app.database.types import UtcDateTime
from app.modules.game.public import ProductVariant
from app.modules.rating.domain.glicko2 import (
    INITIAL_DEVIATION,
    INITIAL_RATING,
    INITIAL_VOLATILITY,
)
from app.modules.rating.domain.keys import SpeedClass

#: database.md §222 — one schema per bounded context.
RATING_SCHEMA = "rating"


def _enum(python_type: type, name: str) -> PgEnum:
    """A native PostgreSQL enum, spelled the way every other one on this
    platform is.

    `values_callable` stores the member *values* rather than the Python
    member names — invisible until somebody queries the table by hand and
    finds `CLASSICAL` where the API said `classical`.

    **Each schema declares its own type**, which is what `matchmaking`
    already does: it has `matchmaking.queue_variant` beside `game`'s
    `game.match_variant` rather than borrowing it. Sharing a type across
    schemas would make a variant added by `game` a migration that locks
    every other context's tables, and would point a `rating` column at a
    type `game` owns and could drop.

    The Python enum is the single source of truth (R-4: one concept, one
    definition), so the two database types cannot list different members.
    """
    return PgEnum(
        python_type,
        name=name,
        schema=RATING_SCHEMA,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


class PlayerRatingModel(Base, TimestampMixin):
    """`rating.player_rating` — the aggregate, one row per key a player has
    actually played.

    **No row until the first rated match** (SPEC-RATING §7.5). A reader that
    finds nothing answers with the initial triple, provisional, zero games,
    so "absent" and "1500/350/0.06, unrated" are one state seen from two
    sides. Creating a row at registration would put one per player per key
    in the table before any of them meant anything.

    Not `UUIDPrimaryKeyMixin`: the identity is `(player, variant, speed
    class)` and a surrogate key would be an identity this aggregate does not
    have — the same argument `PlayerStatisticsModel` makes.
    """

    __tablename__ = "player_rating"

    player_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)

    variant: Mapped[ProductVariant] = mapped_column(
        _enum(ProductVariant, "rating_variant"), primary_key=True
    )
    speed_class: Mapped[SpeedClass] = mapped_column(
        _enum(SpeedClass, "rating_speed_class"), primary_key=True
    )

    rating_value: Mapped[float] = mapped_column(Float, nullable=False, default=INITIAL_RATING)
    rating_deviation: Mapped[float] = mapped_column(
        Float, nullable=False, default=INITIAL_DEVIATION
    )
    rating_volatility: Mapped[float] = mapped_column(
        Float, nullable=False, default=INITIAL_VOLATILITY
    )

    games_played: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Matches that moved **this** rating. Not the player's total — see the
    domain aggregate on why the two legitimately disagree.

    `is_provisional` is **not** a column: it is `games_played < 25`, derived
    on read. A stored flag is a second copy of what this counter already
    says, and the copy is what goes stale.
    """

    is_frozen: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    """PR-5's fair-play hold. Nothing sets it in v0.5.0 — `fairplay` does not
    exist — and that is the documented extension point (SPEC-RATING §13)."""

    peak_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    last_rated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """The clock lazy RD inflation measures from (§7.4). `NULL` until the
    first rated match, which is why inflation is skipped rather than
    computed from the epoch."""

    season_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    """Always `NULL` in v0.5.0 — SPEC-RATING §12. No `Season` entity, no
    reset logic, no foreign key: the column exists only so the data model is
    forward compatible."""

    __table_args__ = (
        # "Who is at the top of this key" — the leaderboard's whole query,
        # and the reason the ordering columns come first. Descending
        # because every reader of a rating reads it from the top.
        #
        # Not unique: two players holding the same rating is the ordinary
        # case, not a conflict.
        # The leaderboard's whole query — A64-017.4. The column order is
        # the `ORDER BY` exactly: an index that omitted `rating_deviation`
        # would serve the scan and then sort the ties, which is a sort per
        # page on the one read a ladder does most.
        #
        # Not unique: two players on the same rating *and* the same
        # deviation is uncommon and legal, which is why `player_id` is
        # there — it makes the order total, and a total order is what makes
        # keyset pagination unable to skip or repeat a player.
        Index(
            "ix_player_rating__standings",
            "variant",
            "speed_class",
            text("rating_value DESC"),
            "rating_deviation",
            "player_id",
        ),
        # --- invariants the aggregate also enforces (BE-06) ----------------
        #
        # Each restates a check `PlayerRating` or `Glicko2Rating` performs,
        # so a row written by a backfill — which does not go through the
        # aggregate — cannot be inconsistent either.
        CheckConstraint("games_played >= 0", name="ck_player_rating__games_not_negative"),
        CheckConstraint("rating_deviation > 0", name="ck_player_rating__deviation_positive"),
        CheckConstraint("rating_volatility > 0", name="ck_player_rating__volatility_positive"),
        # A peak below the present value is an update that missed a step,
        # not a rounding difference.
        CheckConstraint(
            "peak_value IS NULL OR peak_value >= rating_value",
            name="ck_player_rating__peak_is_a_peak",
        ),
        # The two peak columns travel together: a value with no instant is
        # unanswerable when a player asks when they were highest.
        CheckConstraint(
            "(peak_value IS NULL) = (peak_at IS NULL)",
            name="ck_player_rating__peak_is_complete",
        ),
        # A rating that has moved has an instant it last moved at, and one
        # that has not, has not. This is what makes `last_rated_at IS NULL`
        # a reliable "never played" rather than a maybe.
        CheckConstraint(
            "(games_played = 0) = (last_rated_at IS NULL)",
            name="ck_player_rating__played_iff_rated_at",
        ),
        {"schema": RATING_SCHEMA},
    )


class RatingAdjustmentModel(Base, TimestampMixin):
    """`rating.rating_adjustment` — the permanent record of one change.

    **Append-only.** No `updated_at` semantics beyond the mixin's, no
    transition, and no mutating method on the repository: PR-4 makes this
    the answer to "why did I lose 14 points", and an answer that can be
    edited is not one. The stronger form is a runtime role without
    `UPDATE`/`DELETE` on this table, which is a grant rather than a
    migration — recorded, not done.

    Everything needed to explain the change is here, so nothing has to
    re-derive it from an algorithm that may since have been retuned. That
    includes the opponent's whole triple rather than just their rating: the
    deviation is what decided how much weight the result carried.
    """

    __tablename__ = "rating_adjustment"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)

    player_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    match_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    variant: Mapped[ProductVariant] = mapped_column(
        _enum(ProductVariant, "rating_variant"), nullable=False
    )
    speed_class: Mapped[SpeedClass] = mapped_column(
        _enum(SpeedClass, "rating_speed_class"), nullable=False
    )

    rating_before: Mapped[float] = mapped_column(Float, nullable=False)
    deviation_before: Mapped[float] = mapped_column(Float, nullable=False)
    volatility_before: Mapped[float] = mapped_column(Float, nullable=False)

    rating_after: Mapped[float] = mapped_column(Float, nullable=False)
    deviation_after: Mapped[float] = mapped_column(Float, nullable=False)
    volatility_after: Mapped[float] = mapped_column(Float, nullable=False)

    opponent_rating: Mapped[float] = mapped_column(Float, nullable=False)
    opponent_deviation: Mapped[float] = mapped_column(Float, nullable=False)
    opponent_volatility: Mapped[float] = mapped_column(Float, nullable=False)

    expected_score: Mapped[float] = mapped_column(Float, nullable=False)
    actual_score: Mapped[float] = mapped_column(Float, nullable=False)

    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    """Which arithmetic produced these numbers — SPEC-RATING §7.7.

    Without it a retune makes every historical adjustment inexplicable: the
    stored numbers no longer follow from any algorithm the platform can run,
    and the rating history becomes undefendable in a dispute."""

    applied_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    season_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        # **PR-1, and the reason this file exists.** See the module
        # docstring on why two columns rather than three, and why
        # check-then-insert is not an alternative.
        UniqueConstraint("player_id", "match_id", name="uq_rating_adjustment__player_match"),
        # "This player's rating history, newest first" — the query behind
        # "why did I lose 14 points", and the only read this table serves.
        Index(
            "ix_rating_adjustment__player_history",
            "player_id",
            "variant",
            "speed_class",
            text("applied_at DESC"),
        ),
        ForeignKeyConstraint(
            ["player_id", "variant", "speed_class"],
            [
                f"{RATING_SCHEMA}.player_rating.player_id",
                f"{RATING_SCHEMA}.player_rating.variant",
                f"{RATING_SCHEMA}.player_rating.speed_class",
            ],
            name="fk_rating_adjustment__player_rating",
            # **No cascade.** Deleting a rating must not silently delete the
            # record of how it got there — that is the audit trail A-4
            # depends on, and a rating with no explanation is worse than no
            # rating. A player erasure that must reach these rows is
            # DM-13's anonymise-don't-delete decision, not a cascade.
            ondelete="RESTRICT",
        ),
        # A score is a win, a draw or a loss. Not a rating-system rule — no
        # input this platform produces reaches it — but a row written by a
        # backfill with 0.75 in it would be a rating nobody can reproduce.
        CheckConstraint(
            "actual_score IN (0, 0.5, 1)", name="ck_rating_adjustment__score_is_a_result"
        ),
        CheckConstraint(
            "expected_score > 0 AND expected_score < 1",
            name="ck_rating_adjustment__expectation_is_a_probability",
        ),
        {"schema": RATING_SCHEMA},
    )


__all__ = ["RATING_SCHEMA", "PlayerRatingModel", "RatingAdjustmentModel"]
